"""
把抽样后的行为数据清洗、建模成 4 张表，导入 MySQL。

运行方式（在项目根目录 ecom-agent/ 下）：
    python scripts/load_data.py

整个流程（每次运行都会把 4 张表删掉重建，可以放心重复运行）：
    0. 没有 data/sample.csv 就先自动抽样
    1. 读取并清洗：解析时间、去掉完全重复的行
    2. 建模：从事件里拆出 products / categories / sessions
    3. 执行 sql/schema.sql 建表，批量写入数据
    4. 执行 sql/indexes.sql 建索引
    5. 生成派生表 user_first_seen（每个用户第一次出现的日期，用来区分新老用户）
    6. 把清洗过程中的关键数字写入 data/load_report.json，verify_db.py 会用到
    7. 清空分析工具的本地缓存 cache/（数据变了，旧缓存不能再用）

数据量：10–11 月抽样后约 1,100 万行，整个过程大约需要 15–30 分钟。
"""
import json
import shutil
import time

import pandas as pd

from db_utils import ROOT, get_conn, run_sql_file

SAMPLE_PATH = ROOT / "data" / "sample.csv"
REPORT_PATH = ROOT / "data" / "load_report.json"
BATCH_SIZE = 20_000

# 字段长度上限，必须和 sql/schema.sql 里的 VARCHAR 长度一致
MAX_LEN = {"brand": 100, "category_code": 255, "level1": 100, "level2": 100, "user_session": 64}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 0 + 1. 读取与清洗
# ---------------------------------------------------------------------------
def load_and_clean() -> tuple[pd.DataFrame, dict]:
    if not SAMPLE_PATH.exists():
        log("没有找到 data/sample.csv，先运行抽样（全量文件较大，需要几分钟）")
        import sample_data
        sample_data.main()

    log("读取 data/sample.csv ...")
    df = pd.read_csv(
        SAMPLE_PATH,
        dtype={
            "event_type": "string",
            "product_id": "int64",
            "category_id": "int64",
            "category_code": "string",
            "brand": "string",
            "price": "float64",
            "user_id": "int64",
            "user_session": "string",
        },
    )
    report = {"sample_rows": len(df), "sample_users": int(df["user_id"].nunique())}
    log(f"读入 {len(df):,} 行，{report['sample_users']:,} 个用户")

    # 时间：原始格式是 "2019-10-01 00:00:04 UTC"，转成不带时区的 datetime（数值上仍是 UTC）
    df["event_time"] = pd.to_datetime(df["event_time"], format="%Y-%m-%d %H:%M:%S UTC")

    # 完全重复的行：所有字段（精确到秒）都一样，判断为日志重复上报，删除
    n_dup = int(df.duplicated().sum())
    df = df.drop_duplicates().reset_index(drop=True)
    report["duplicate_rows_removed"] = n_dup
    log(f"删除完全重复的行 {n_dup:,} 行，剩余 {len(df):,} 行")

    # 只记录、不删除的数据问题（写进说明书，让 Agent 在计算时自己处理）
    report["price_le_zero_rows"] = int((df["price"] <= 0).sum())
    report["session_null_rows"] = int(df["user_session"].isna().sum())
    report["event_types"] = {k: int(v) for k, v in df["event_type"].value_counts().items()}
    report["time_min"] = str(df["event_time"].min())
    report["time_max"] = str(df["event_time"].max())
    return df, report


# ---------------------------------------------------------------------------
# 2. 建模：一张事件大表 → 4 张表
# ---------------------------------------------------------------------------
def mode_per_key(df: pd.DataFrame, key: str, col: str) -> pd.Series:
    """对每个 key，取 col 出现次数最多的非空值；次数相同时取字母序最小的，保证结果可复现。"""
    counts = (
        df.dropna(subset=[col])
        .groupby([key, col])
        .size()
        .reset_index(name="n")
        .sort_values([key, "n", col], ascending=[True, False, True])
        .drop_duplicates(key)
    )
    return counts.set_index(key)[col]


def build_products(df, report):
    products = pd.DataFrame(index=pd.Index(df["product_id"].unique(), name="product_id"))
    products["category_id"] = mode_per_key(df, "product_id", "category_id")
    products["brand"] = mode_per_key(df, "product_id", "brand")  # 全部为空的商品得到 NaN → NULL

    n_brand = df.groupby("product_id")["brand"].nunique()
    report["products"] = len(products)
    report["products_brand_conflict"] = int((n_brand > 1).sum())
    report["products_brand_null"] = int(products["brand"].isna().sum())
    report["products_category_conflict"] = int((df.groupby("product_id")["category_id"].nunique() > 1).sum())
    return products.reset_index()


def build_categories(df, report):
    categories = pd.DataFrame(index=pd.Index(df["category_id"].unique(), name="category_id"))
    categories["category_code"] = mode_per_key(df, "category_id", "category_code")
    parts = categories["category_code"].str.split(".", n=2, expand=True)
    categories["level1"] = parts[0]
    categories["level2"] = parts[1] if 1 in parts.columns else pd.NA

    report["categories"] = len(categories)
    report["categories_code_null"] = int(categories["category_code"].isna().sum())
    report["categories_code_conflict"] = int((df.groupby("category_id")["category_code"].nunique() > 1).sum())
    return categories.reset_index()


def build_sessions(df, report):
    """按会话汇总。计数用「先算 0/1 标记再求和」，比在 agg 里写 lambda 快很多。"""
    s = df.dropna(subset=["user_session"]).sort_values("event_time", kind="stable")
    report["sessions_multi_user"] = int((s.groupby("user_session")["user_id"].nunique() > 1).sum())
    flags = pd.DataFrame({
        "user_session": s["user_session"],
        "n_views": (s["event_type"] == "view").astype("int64"),
        "n_carts": (s["event_type"] == "cart").astype("int64"),
        "n_purchases": (s["event_type"] == "purchase").astype("int64"),
    })
    sessions = s.groupby("user_session").agg(
        user_id=("user_id", "first"),          # 极少数会话对应多个用户时，取最早出现的那个
        start_time=("event_time", "min"),
        end_time=("event_time", "max"),
    ).join(flags.groupby("user_session").sum())
    report["sessions"] = len(sessions)
    return sessions.reset_index()


# ---------------------------------------------------------------------------
# 3. 写入 MySQL
# ---------------------------------------------------------------------------
def to_rows(frame: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """DataFrame → [(值, 值, ...), ...]，缺失值换成 None（写进 MySQL 就是 NULL），时间换成字符串。"""
    columns = []
    for c in cols:
        col = frame[c]
        if pd.api.types.is_datetime64_any_dtype(col):
            values = col.dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
        else:
            values = col.astype(object).where(col.notna(), None).tolist()
        columns.append(values)
    return list(zip(*columns))


def insert_table(conn, table: str, frame: pd.DataFrame, cols: list[str]):
    """分批写入。每批只把 BATCH_SIZE 行转成 Python 元组，
    不一次性转换整张表（1,100 万行全部转成元组会占用好几 GB 内存）。"""
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    total = len(frame)
    start = time.time()
    with conn.cursor() as cur:
        for i in range(0, total, BATCH_SIZE):
            rows = to_rows(frame.iloc[i:i + BATCH_SIZE], cols)
            cur.executemany(sql, rows)  # pymysql 会合并成多行 INSERT，比逐行快很多
            conn.commit()
            done = min(i + BATCH_SIZE, total)
            if done == total or (i // BATCH_SIZE) % 25 == 0:
                log(f"  {table}: {done:,} / {total:,} 行（{time.time() - start:.0f} 秒）")


def check_lengths(frames: dict):
    """写入前检查字符串长度，超长的话 MySQL 会直接报错，这里先给出清楚的提示。"""
    for name, frame in frames.items():
        for col, limit in MAX_LEN.items():
            if col in frame.columns:
                longest = frame[col].dropna().str.len().max()
                if pd.notna(longest) and longest > limit:
                    raise ValueError(f"{name}.{col} 最长 {longest} 个字符，超过建表时的 {limit}，请改大 schema.sql")


def main():
    t0 = time.time()
    df, report = load_and_clean()

    log("建模：products / categories / sessions ...")
    products = build_products(df, report)
    categories = build_categories(df, report)
    sessions = build_sessions(df, report)
    events = df.sort_values("event_time", kind="stable")  # 按时间排序后写入，event_id 大致随时间递增
    log(f"products {len(products):,} 行，categories {len(categories):,} 行，sessions {len(sessions):,} 行")
    check_lengths({"events": events, "products": products, "categories": categories, "sessions": sessions})

    conn = get_conn("admin")
    try:
        log("执行 sql/schema.sql 建表（会删除旧表）...")
        run_sql_file(conn, ROOT / "sql" / "schema.sql")

        log("写入数据 ...")
        insert_table(conn, "categories", categories, ["category_id", "category_code", "level1", "level2"])
        insert_table(conn, "products", products, ["product_id", "category_id", "brand"])
        insert_table(conn, "sessions", sessions,
                     ["user_session", "user_id", "start_time", "end_time", "n_views", "n_carts", "n_purchases"])
        insert_table(conn, "events", events,
                     ["event_time", "event_type", "product_id", "price", "user_id", "user_session"])

        log("执行 sql/indexes.sql 建索引（events 表较大，需要几分钟）...")
        run_sql_file(conn, ROOT / "sql" / "indexes.sql")

        log("生成 user_first_seen（每个用户第一次出现的日期）...")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_first_seen (user_id, first_date)
                SELECT user_id, MIN(DATE(event_time)) FROM events GROUP BY user_id""")
            report["users_first_seen"] = cur.rowcount
            conn.commit()
            cur.execute("ANALYZE TABLE events, products, categories, sessions, user_first_seen")  # 更新统计信息
            cur.fetchall()
    finally:
        conn.close()

    # 分析工具会把取过的数据缓存到 cache/，重新导入后必须清掉
    shutil.rmtree(ROOT / "cache", ignore_errors=True)

    report["events_loaded"] = len(events)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ 完成，总用时 {time.time() - t0:.0f} 秒。清洗记录已写入 {REPORT_PATH.relative_to(ROOT)}")
    log("下一步：python scripts/verify_db.py")


if __name__ == "__main__":
    main()

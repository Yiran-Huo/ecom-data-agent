"""
验收数据库，并生成 Agent 用的数据概况 agent/data_profile.md。

运行方式（在项目根目录 ecom-agent/ 下，先跑完 load_data.py）：
    python scripts/verify_db.py

全程用 Agent 的只读账号连接：Agent 能看到什么，这里就检查什么。
    1. 只读账号：能查询，不能建表、不能删数据
    2. 数据完整性：表之间能对上，没有孤立的记录
    3. 数据概况：关键数字写入 agent/data_profile.md，之后放进 System Prompt
"""
import json
import sys
import time

import pymysql

from db_utils import ROOT, get_conn

REPORT_PATH = ROOT / "data" / "load_report.json"
PROFILE_PATH = ROOT / "agent" / "data_profile.md"

failures = []


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}{('：' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def one(cur, sql):
    """执行一条查询，返回第一行第一列。"""
    cur.execute(sql)
    return cur.fetchone()[0]


def check_read_only(cur):
    print("\n1. 只读账号")
    check("可以查询", one(cur, "SELECT COUNT(*) FROM categories") >= 0)
    for name, sql in [
        ("不能建表", "CREATE TABLE _should_fail (x INT)"),
        ("不能删数据", "DELETE FROM categories WHERE 1 = 0"),
        ("不能删表", "DROP TABLE categories"),
    ]:
        try:
            cur.execute(sql)
            check(name, False, "居然执行成功了！检查 GRANT 是否只给了 SELECT")
        except pymysql.err.MySQLError as e:
            check(name, e.args[0] in (1142, 1044), f"被拒绝（错误码 {e.args[0]}）")


def check_integrity(cur):
    print("\n2. 数据完整性")
    counts = {t: one(cur, f"SELECT COUNT(*) FROM {t}") for t in ["events", "products", "categories", "sessions"]}
    for t, n in counts.items():
        check(f"{t} 有数据", n > 0, f"{n:,} 行")

    if REPORT_PATH.exists():
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        check("events 行数与导入记录一致", counts["events"] == report["events_loaded"],
              f"库里 {counts['events']:,}，导入时 {report['events_loaded']:,}")

    orphan_products = one(cur, """
        SELECT COUNT(*) FROM events e LEFT JOIN products p ON e.product_id = p.product_id
        WHERE p.product_id IS NULL""")
    check("每个事件的商品都在 products 里", orphan_products == 0, f"孤立 {orphan_products}")

    orphan_cats = one(cur, """
        SELECT COUNT(*) FROM products p LEFT JOIN categories c ON p.category_id = c.category_id
        WHERE c.category_id IS NULL""")
    check("每个商品的品类都在 categories 里", orphan_cats == 0, f"孤立 {orphan_cats}")

    orphan_sessions = one(cur, """
        SELECT COUNT(*) FROM events e LEFT JOIN sessions s ON e.user_session = s.user_session
        WHERE e.user_session IS NOT NULL AND s.user_session IS NULL""")
    check("每个非空会话都在 sessions 里", orphan_sessions == 0, f"孤立 {orphan_sessions}")

    p_sessions = one(cur, "SELECT COALESCE(SUM(n_purchases), 0) FROM sessions")
    p_events = one(cur, "SELECT COUNT(*) FROM events WHERE event_type = 'purchase' AND user_session IS NOT NULL")
    check("sessions 汇总的购买数与 events 一致", int(p_sessions) == p_events, f"{int(p_sessions):,} vs {p_events:,}")

    n_first_seen = one(cur, "SELECT COUNT(*) FROM user_first_seen")
    n_users = one(cur, "SELECT COUNT(DISTINCT user_id) FROM events")
    check("user_first_seen 覆盖全部用户", n_first_seen == n_users, f"{n_first_seen:,} vs {n_users:,}")

    idx = one(cur, """
        SELECT COUNT(DISTINCT index_name) FROM information_schema.statistics
        WHERE table_schema = DATABASE() AND index_name LIKE 'idx_%'""")
    check("索引已建好", idx >= 7, f"{idx} 个")
    return counts


def build_profile(cur, counts):
    print("\n3. 生成数据概况 ...")
    q = lambda sql: (cur.execute(sql), cur.fetchall())[1]

    t_min, t_max = q("SELECT MIN(event_time), MAX(event_time) FROM events")[0]
    by_type = q("""
        SELECT event_type, COUNT(*), COUNT(DISTINCT user_id)
        FROM events GROUP BY event_type ORDER BY COUNT(*) DESC""")
    users = one(cur, "SELECT COUNT(DISTINCT user_id) FROM events")
    gmv, n_buy, avg_price = q("""
        SELECT SUM(price), COUNT(*), AVG(price) FROM events
        WHERE event_type = 'purchase' AND price > 0""")[0]
    bad_price = q("SELECT event_type, COUNT(*) FROM events WHERE price <= 0 GROUP BY event_type")
    brand_null_products = one(cur, "SELECT AVG(brand IS NULL) FROM products")
    brand_null_purchases = one(cur, """
        SELECT AVG(p.brand IS NULL) FROM events e JOIN products p ON e.product_id = p.product_id
        WHERE e.event_type = 'purchase'""")
    code_null_purchases = one(cur, """
        SELECT AVG(c.category_code IS NULL) FROM events e
        JOIN products p ON e.product_id = p.product_id
        JOIN categories c ON p.category_id = c.category_id
        WHERE e.event_type = 'purchase'""")
    session_null = one(cur, "SELECT COUNT(*) FROM events WHERE user_session IS NULL")
    buy_no_cart_sessions = one(cur, "SELECT AVG(n_carts = 0) FROM sessions WHERE n_purchases > 0")
    multi_buyers = one(cur, """
        SELECT COUNT(*) FROM (
            SELECT user_id FROM events WHERE event_type = 'purchase' AND price > 0
            GROUP BY user_id HAVING COUNT(DISTINCT user_session) >= 2) t""")
    buyers = one(cur, "SELECT COUNT(DISTINCT user_id) FROM events WHERE event_type = 'purchase' AND price > 0")
    top_l1 = q("""
        SELECT COALESCE(c.level1, '(无品类)') AS level1, COUNT(*) AS n
        FROM events e JOIN products p ON e.product_id = p.product_id
        JOIN categories c ON p.category_id = c.category_id
        WHERE e.event_type = 'purchase'
        GROUP BY level1 ORDER BY n DESC LIMIT 8""")

    report = json.loads(REPORT_PATH.read_text(encoding="utf-8")) if REPORT_PATH.exists() else {}
    user_by_type = {t: u for t, _, u in by_type}
    pct = lambda a, b: f"{a / b:.1%}" if b else "-"

    lines = [
        "# 数据概况（自动生成）",
        "",
        f"> 由 `scripts/verify_db.py` 于 {time.strftime('%Y-%m-%d %H:%M')} 根据数据库实际内容生成，"
        "不要手动修改；重新导入数据后重新运行即可更新。",
        "> 以下都是**抽样数据**（约全量用户的 1/10）的数字，绝对数不能当全量报告。",
        "",
        "## 规模",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 时间范围（UTC） | {t_min} ~ {t_max} |",
        f"| 事件数（去重后） | {counts['events']:,} |",
        f"| 用户数 | {users:,} |",
        f"| 会话数 | {counts['sessions']:,} |",
        f"| 商品数 | {counts['products']:,} |",
        f"| 品类数（category_id） | {counts['categories']:,} |",
        "",
        "## 事件类型",
        "",
        "| event_type | 事件数 | 用户数 |",
        "| --- | --- | --- |",
        *[f"| {t} | {n:,} | {u:,} |" for t, n, u in by_type],
        "",
        "## 关键指标（参考值，用于自检）",
        "",
        "| 指标 | 数值 | 口径 |",
        "| --- | --- | --- |",
        f"| GMV | {float(gmv or 0):,.2f} | purchase 且 price > 0 的 price 之和 |",
        f"| 有效购买次数 | {n_buy:,} | purchase 且 price > 0 |",
        f"| 平均成交价 | {float(avg_price or 0):,.2f} | 同上的平均 price |",
        f"| 用户级 浏览→购买 转化率 | {pct(user_by_type.get('purchase', 0), user_by_type.get('view', 0))} | 有购买的用户数 ÷ 有浏览的用户数 |",
        f"| 用户级 浏览→加购 转化率 | {pct(user_by_type.get('cart', 0), user_by_type.get('view', 0))} | 有加购的用户数 ÷ 有浏览的用户数 |",
        f"| 复购用户数 | {multi_buyers:,} | 在 ≥2 个不同会话中有有效购买的用户 |",
        f"| 复购率 | {pct(multi_buyers, buyers)} | 复购用户数 ÷ 有有效购买的用户数 |",
        "",
        "## 数据质量",
        "",
        "| 问题 | 数值 | 处理方式 |",
        "| --- | --- | --- |",
        f"| 导入时删除的完全重复行 | {report.get('duplicate_rows_removed', '未知')} | 已删除 |",
        *[f"| price ≤ 0 的 {t} 事件 | {n:,} | 保留在表中；计算金额类指标时排除 |" for t, n in bad_price],
        f"| user_session 为空的事件 | {session_null:,} | 保留；这些事件不在 sessions 表中 |",
        f"| 品牌为空的商品占比 | {float(brand_null_products):.1%} | 按品牌统计时需说明 |",
        f"| 购买事件中品牌为空的占比 | {float(brand_null_purchases or 0):.1%} | 同上 |",
        f"| 购买事件中 category_code 为空的占比 | {float(code_null_purchases or 0):.1%} | 按品类统计时需说明 |",
        f"| 有购买但没有加购的会话占比 | {float(buy_no_cart_sessions or 0):.1%} | 加购→购买转化率不能直接相除 |",
        f"| 品牌不一致的商品（导入时取众数） | {report.get('products_brand_conflict', '未知')} | 取出现次数最多的品牌 |",
        f"| 对应多个用户的会话 | {report.get('sessions_multi_user', '未知')} | 取最早出现的用户 |",
        "",
        "## 购买次数最多的一级品类（前 8）",
        "",
        "| level1 | 购买事件数 |",
        "| --- | --- |",
        *[f"| {l1} | {n:,} |" for l1, n in top_l1],
        "",
    ]
    PROFILE_PATH.parent.mkdir(exist_ok=True)
    PROFILE_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"  已写入 {PROFILE_PATH.relative_to(ROOT)}")


def main():
    conn = get_conn("ro")  # 用 Agent 的只读账号
    try:
        with conn.cursor() as cur:
            check_read_only(cur)
            counts = check_integrity(cur)
            build_profile(cur, counts)
    finally:
        conn.close()

    if failures:
        sys.exit(f"\n❌ 有 {len(failures)} 项没通过：{', '.join(failures)}")
    print("\n✅ 全部通过，数据库可以交给 Agent 使用了")


if __name__ == "__main__":
    main()

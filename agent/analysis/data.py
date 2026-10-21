"""
取数层：分析工具需要的数据都从这里拿。

两种取数方式：
1. load_window(day)：取「当天 + 前 7 天」的全部事件（瘦身后的几列），加上维度后放进 pandas。
   之后的指标计算、拆解、下钻全部在 pandas 里做，同一个日期只查一次数据库。
   - 进程内缓存：同一次运行里多次调用不重复查询
   - 磁盘缓存 cache/frames/<库名>/<日期>.parquet：评测重复跑时不重复查询
   - 重新导入数据（load_data.py）或重建场景（build_scenarios.py）时会自动清掉缓存
2. daily_series_sql(start, end, filters)：较长时间范围的每日指标，直接在 MySQL 里聚合。

这个文件里只有 fetch_* 和 daily_series_sql 会连数据库；enrich、normalize_filters 是纯函数，可以单元测试。
"""
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from agent.analysis import config as C

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / "cache" / "frames"
QUERY_TIMEOUT_MS = 180_000   # 分析查询比 run_sql 重，给 3 分钟

_memory_cache: dict = {}     # (库名, 日期) -> 加好维度的 DataFrame
_dims_cache: dict = {}       # 库名 -> 商品维度表

EVENT_CODES = {"view": 0, "cart": 1, "purchase": 2}


# ---------------------------------------------------------------------------
# 日期和过滤条件的检查（纯函数）
# ---------------------------------------------------------------------------
def parse_day(value) -> date:
    """'2019-10-24' → date，并检查能不能排查（要有 7 天基线）。不合法时抛 ValueError，工具层会转成错误提示。"""
    try:
        day = value if isinstance(value, date) else date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"日期格式不对：{value}，请用 YYYY-MM-DD，例如 2019-10-24")
    if not (C.FIRST_DIAGNOSABLE <= day <= C.DATA_END):
        raise ValueError(f"只能排查 {C.FIRST_DIAGNOSABLE} 至 {C.DATA_END} 之间的日期"
                         f"（数据从 {C.DATA_START} 开始，需要前 {C.BASELINE_DAYS} 天做基线）")
    return day


def normalize_filters(filters: dict | None) -> dict:
    """检查过滤条件：维度必须在白名单里；去掉空值；hour 转成整数；字符串统一小写（品牌、品类在库里都是小写）。"""
    out = {}
    for dim, value in (filters or {}).items():
        if value is None or value == "":
            continue
        if dim not in C.DIMENSIONS:
            raise ValueError(f"没有维度 {dim}，可用的维度：{', '.join(C.DIMENSIONS)}")
        if dim == "hour":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"hour 要写 0–23 的整数，收到的是 {value}")
            if not 0 <= value <= 23:
                raise ValueError(f"hour 要写 0–23 的整数，收到的是 {value}")
        elif dim == "user_type":
            value = str(value).strip().lower()
            value = {"新用户": "new", "老用户": "returning", "old": "returning"}.get(value, value)
            if value not in C.USER_TYPES:
                raise ValueError("user_type 只能是 new（新用户）或 returning（老用户）")
        else:
            value = str(value).strip()
            if value not in C.NULL_LABELS.values() and dim != "price_band":
                value = value.lower()
        out[dim] = value
    return out


# ---------------------------------------------------------------------------
# 加维度（纯函数）
# ---------------------------------------------------------------------------
def price_band_of(price: pd.Series) -> pd.Series:
    """成交价 → 价格段名称（左闭右开）。"""
    edges = [lo for lo, _, _ in C.PRICE_BANDS] + [np.inf]
    labels = [name for _, _, name in C.PRICE_BANDS]
    return pd.cut(price.clip(lower=0), bins=edges, labels=labels, right=False)


def reference_price_band(frame: pd.DataFrame, day: date) -> pd.Series:
    """
    价格段按商品的「参考价」划分，而不是按每一笔的成交价。
    参考价 = 这个商品在基线 7 天里的价格中位数（基线里没出现过的新商品，用它当天的价格中位数）。

    为什么：如果按成交价分段，某品牌降价 40% 时，它的商品会从高价段「跳」到低价段，
    价格段维度会显示「高价段暴跌、低价段上涨」，看起来像是价格段出了问题，其实只是降价的副作用。
    按参考价分段，价格段就是商品的固定属性，降价只体现在 P（平均成交价）这个因子上。
    """
    in_base = frame["d"] < pd.Timestamp(day)
    ref = frame[in_base].groupby("product_id")["price"].median()
    ref_today = frame[~in_base].groupby("product_id")["price"].median()
    ref = ref.combine_first(ref_today)
    return price_band_of(frame["product_id"].map(ref).fillna(frame["price"]))


def enrich(raw: pd.DataFrame, dims: pd.DataFrame, day: date | None = None) -> pd.DataFrame:
    """
    给事件加上维度列。
    raw 需要的列：d（日期）、h（小时）、et（0 浏览 / 1 加购 / 2 购买）、user_id、product_id、price、is_new（0/1）
    dims 需要的列：product_id、level1、level2、brand
    day：排查的日期。给了就按参考价划价格段（推荐），不给就按成交价。
    """
    frame = raw.merge(dims, on="product_id", how="left")
    for dim, label in C.NULL_LABELS.items():
        frame[dim] = frame[dim].fillna(label).astype("category")
    frame["d"] = pd.to_datetime(frame["d"]).dt.normalize()
    frame["price_band"] = reference_price_band(frame, day) if day else price_band_of(frame["price"])
    frame["user_type"] = np.where(frame["is_new"].astype(bool), "new", "returning")
    frame["user_type"] = frame["user_type"].astype("category")
    frame["hour"] = frame["h"].astype("int8")
    return frame


# ---------------------------------------------------------------------------
# 连数据库的部分
# ---------------------------------------------------------------------------
def _read_sql(sql: str, params: dict, database: str | None) -> pd.DataFrame:
    from sqlalchemy import text

    from agent.db import get_engine   # 放在函数里导入：单元测试不需要数据库

    with get_engine(database).connect() as conn:
        conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {QUERY_TIMEOUT_MS}"))
        return pd.read_sql(text(sql), conn, params=params)


def _db_name(database: str | None) -> str:
    from agent.db import current_database
    return database or current_database()


def fetch_window(day: date, database: str | None = None) -> pd.DataFrame:
    """取 [day-7, day] 共 8 天的事件，只取分析需要的几列。"""
    start = day - timedelta(days=C.BASELINE_DAYS)
    end = day + timedelta(days=1)
    sql = """
        SELECT DATE(e.event_time) AS d,
               HOUR(e.event_time) AS h,
               CASE e.event_type WHEN 'view' THEN 0 WHEN 'cart' THEN 1 ELSE 2 END AS et,
               e.user_id, e.product_id, e.price,
               (u.first_date = DATE(e.event_time)) AS is_new
        FROM events e
        LEFT JOIN user_first_seen u ON u.user_id = e.user_id
        WHERE e.event_time >= :start AND e.event_time < :end
    """
    raw = _read_sql(sql, {"start": start.isoformat(), "end": end.isoformat()}, database)
    raw["price"] = raw["price"].astype("float64")
    raw["et"] = raw["et"].astype("int8")
    raw["is_new"] = raw["is_new"].fillna(0).astype("int8")
    return raw


def fetch_dims(database: str | None = None) -> pd.DataFrame:
    name = _db_name(database)
    if name not in _dims_cache:
        _dims_cache[name] = _read_sql("""
            SELECT p.product_id, c.level1, c.level2, p.brand
            FROM products p JOIN categories c ON c.category_id = p.category_id
        """, {}, name)
    return _dims_cache[name]


def load_window(day, database: str | None = None) -> pd.DataFrame:
    """当天 + 前 7 天、加好维度的事件表。先查内存缓存，再查磁盘缓存，最后才查数据库。"""
    day = parse_day(day)
    name = _db_name(database)
    key = (name, day)
    if key in _memory_cache:
        return _memory_cache[key]

    path = CACHE_DIR / name / f"{day.isoformat()}.parquet"
    if path.exists():
        frame = pd.read_parquet(path)
        for col in ["level1", "level2", "brand", "price_band", "user_type"]:
            frame[col] = frame[col].astype("category")
    else:
        frame = enrich(fetch_window(day, name), fetch_dims(name), day)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
    _memory_cache[key] = frame
    return frame


def clear_cache(database: str | None = None):
    """清缓存。不传参数就全部清掉。"""
    if database is None:
        _memory_cache.clear()
        _dims_cache.clear()
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        return
    for key in [k for k in _memory_cache if k[0] == database]:
        del _memory_cache[key]
    _dims_cache.pop(database, None)
    shutil.rmtree(CACHE_DIR / database, ignore_errors=True)


# ---------------------------------------------------------------------------
# 较长时间范围：每日指标直接在 MySQL 里聚合
# ---------------------------------------------------------------------------
def _price_band_sql(col: str = "e.price") -> str:
    parts = [f"WHEN {col} < {hi} THEN '{name}'" for lo, hi, name in C.PRICE_BANDS if hi != float("inf")]
    return f"CASE {' '.join(parts)} ELSE '{C.PRICE_BANDS[-1][2]}' END"


DIM_SQL = {
    "level1": "c.level1",
    "level2": "c.level2",
    "brand": "p.brand",
    "price_band": _price_band_sql(),
    "user_type": "CASE WHEN u.first_date = DATE(e.event_time) THEN 'new' ELSE 'returning' END",
    "hour": "HOUR(e.event_time)",
}


def filter_sql(filters: dict) -> tuple[str, dict]:
    """过滤条件 → SQL 片段和参数。维度名来自白名单，取值全部走参数，不拼进 SQL。"""
    clauses, params = [], {}
    for i, (dim, value) in enumerate(normalize_filters(filters).items()):
        if value == C.NULL_LABELS.get(dim):
            clauses.append(f"{DIM_SQL[dim]} IS NULL")
        else:
            clauses.append(f"{DIM_SQL[dim]} = :f{i}")
            params[f"f{i}"] = value
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def daily_series_sql(start: date, end: date, filters: dict | None = None,
                     database: str | None = None) -> pd.DataFrame:
    """[start, end] 每天的 UV、购买用户数、有效购买次数、GMV（在 MySQL 里聚合）。"""
    if "price_band" in (filters or {}):
        raise ValueError("get_daily_series 不支持按 price_band 过滤（价格段按商品参考价划分，只在单日分析工具里可用）")
    where, params = filter_sql(filters or {})
    params.update({"start": start.isoformat(), "end": (end + timedelta(days=1)).isoformat()})
    valid = "e.event_type = 'purchase' AND e.price > 0"
    # 只在过滤条件用到时才 join，全站查询直接扫 events，快很多
    used = set(filters or {})
    joins = ""
    if used & {"brand", "level1", "level2"}:
        joins += " JOIN products p ON p.product_id = e.product_id"
    if used & {"level1", "level2"}:
        joins += " JOIN categories c ON c.category_id = p.category_id"
    if "user_type" in used:
        joins += " LEFT JOIN user_first_seen u ON u.user_id = e.user_id"
    sql = f"""
        SELECT DATE(e.event_time) AS d,
               COUNT(DISTINCT CASE WHEN e.event_type = 'view' THEN e.user_id END) AS uv,
               COUNT(DISTINCT CASE WHEN {valid} THEN e.user_id END) AS buyers,
               SUM({valid}) AS n,
               SUM(CASE WHEN {valid} THEN e.price ELSE 0 END) AS gmv
        FROM events e {joins}
        WHERE e.event_time >= :start AND e.event_time < :end {where}
        GROUP BY d ORDER BY d
    """
    df = _read_sql(sql, params, database)
    df["d"] = pd.to_datetime(df["d"])
    for col in ["uv", "buyers", "n"]:
        df[col] = df[col].fillna(0).astype("int64")
    df["gmv"] = df["gmv"].fillna(0).astype("float64")
    return df

"""
场景注入：把「场景定义」翻译成 MySQL 视图（纯函数，只生成 SQL 字符串，不连数据库）。

每个场景是一个独立的库 ecom_s_<id>，里面的 events 是对真实 events 的视图，只在「排查日」当天做改动：
    删除   WHERE NOT (条件)                              例：删掉某品牌当天 60% 的购买
    改价   price = CASE WHEN 条件 THEN price × 系数 END   例：某品牌当天降价 30%
    复制   JOIN 一张只有 k=1,2 两行的小表，满足条件的行多复制一份   例：某品牌当天购买翻倍（促销）
不复制 1,100 万行数据，建场景几乎不占空间；Agent 看到的表名完全一样，不知道自己在被测。

按比例抽取：
    按事件抽 MOD(event_id, 100) < k
    按用户抽 MOD(FLOOR(user_id / 10), 100) < k
    （样本本身是按 user_id % 10 = 0 抽的，直接 MOD(user_id, 100) 只会取到 0、10、20……，比例会不对）
"""
from datetime import date, timedelta

META_DB = "scn_meta"   # 放复制用的小表 mult(k)


def quote(value) -> str:
    """字符串常量转义（取值来自数据库里的品牌 / 品类名，不来自用户输入，但还是转义一下）。"""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def day_cond(day: date) -> str:
    nxt = day + timedelta(days=1)
    return f"(e.event_time >= '{day.isoformat()}' AND e.event_time < '{nxt.isoformat()}')"


def filter_cond(f: dict, day: date) -> str:
    parts = []
    if "brand" in f:
        parts.append(f"p.brand = {quote(f['brand'])}")
    if "level1" in f:
        parts.append(f"c.level1 = {quote(f['level1'])}")
    if "level2" in f:
        parts.append(f"c.level2 = {quote(f['level2'])}")
    if f.get("new_user"):
        parts.append(f"u.first_date = '{day.isoformat()}'")
    if "min_price" in f:
        parts.append(f"e.price >= {float(f['min_price'])}")
    if "hours" in f:
        parts.append(f"HOUR(e.event_time) IN ({', '.join(str(int(h)) for h in f['hours'])})")
    return " AND ".join(parts) or "TRUE"


def sample_cond(share: float, unit: str) -> str:
    k = int(round(share * 100))
    if k >= 100:
        return "TRUE"
    col = "e.event_id" if unit == "event" else "FLOOR(e.user_id / 10)"
    return f"MOD({col}, 100) < {k}"


def compile_ops(ops: list[dict], day: date) -> dict:
    """
    ops：[{op: delete | price | duplicate, filter: {...}, scope: purchase | all, share, unit: event | user, factor}]
    返回 {"delete": SQL 条件, "price": SQL 表达式, "duplicate": SQL 条件}
    """
    deletes, prices, dups = [], [], []
    for op in ops:
        conds = [day_cond(day), filter_cond(op.get("filter", {}), day)]
        if op.get("scope", "all") == "purchase":
            conds.append("e.event_type = 'purchase'")
        if op["op"] in ("delete", "duplicate"):
            conds.append(sample_cond(op.get("share", 1.0), op.get("unit", "event")))
        cond = " AND ".join(c for c in conds if c != "TRUE")
        if op["op"] == "delete":
            deletes.append(f"({cond})")
        elif op["op"] == "duplicate":
            dups.append(f"({cond})")
        elif op["op"] == "price":
            prices.append(f"WHEN {cond} THEN ROUND(e.price * {float(op['factor'])}, 2)")
        else:
            raise ValueError(f"未知的注入操作 {op['op']}")
    return {
        "delete": " OR ".join(deletes) or "FALSE",
        "price": f"CASE {' '.join(prices)} ELSE e.price END" if prices else "e.price",
        "duplicate": " OR ".join(dups) or "FALSE",
    }


def view_statements(db: str, base: str, ops: list[dict], day: date) -> list[str]:
    """建一个场景库需要执行的全部 SQL。"""
    sql = compile_ops(ops, day)
    stmts = [
        f"DROP DATABASE IF EXISTS `{db}`",
        f"CREATE DATABASE `{db}` DEFAULT CHARSET utf8mb4",
        f"""CREATE VIEW `{db}`.`events` AS
SELECT e.event_id + (m.k - 1) * 1000000000 AS event_id,
       e.event_time, e.event_type, e.product_id,
       CAST({sql['price']} AS DECIMAL(10,2)) AS price,
       e.user_id, e.user_session
FROM `{base}`.`events` e
JOIN `{base}`.`products` p ON p.product_id = e.product_id
JOIN `{base}`.`categories` c ON c.category_id = p.category_id
LEFT JOIN `{base}`.`user_first_seen` u ON u.user_id = e.user_id
JOIN `{META_DB}`.`mult` m ON (m.k = 1 OR ({sql['duplicate']}))
WHERE NOT ({sql['delete']})""",
    ]
    for table in ["products", "categories", "sessions", "user_first_seen"]:
        stmts.append(f"CREATE VIEW `{db}`.`{table}` AS SELECT * FROM `{base}`.`{table}`")
    return stmts


def meta_statements() -> list[str]:
    return [
        f"CREATE DATABASE IF NOT EXISTS `{META_DB}`",
        f"CREATE TABLE IF NOT EXISTS `{META_DB}`.`mult` (k TINYINT PRIMARY KEY)",
        f"INSERT IGNORE INTO `{META_DB}`.`mult` VALUES (1), (2)",
    ]

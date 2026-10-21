"""
合成数据：给分析工具做单元测试用，不需要数据库。

造一个小电商：6 个品牌、3 个一级品类，8 天（基线 7 天 + 当天），每天约 4,000 个访客。
每个访客浏览 1–4 个商品，按商品自己的转化率决定买不买。各天之间只有随机波动，没有异常。
测试里再用 inject_* 函数在「当天」注入已知原因的异动，检查工具能不能找出来。
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from agent.analysis.data import enrich

DAY = date(2019, 10, 20)

# 商品：品牌、一级 / 二级品类、基础价格、热度、转化率
PRODUCTS = [
    # brand,    level1,        level2,       price, popularity, cvr
    ("alpha",   "electronics", "smartphone", 600,   10,         0.10),
    ("alpha",   "electronics", "smartphone", 900,   6,          0.08),
    ("beta",    "electronics", "smartphone", 300,   8,          0.10),
    ("beta",    "electronics", "tv",         450,   4,          0.06),
    ("gamma",   "appliances",  "kitchen",    120,   8,          0.12),
    ("gamma",   "appliances",  "kitchen",    80,    6,          0.14),
    ("delta",   "appliances",  "washer",     350,   4,          0.07),
    ("epsilon", "computers",   "notebook",   1200,  4,          0.05),
    ("epsilon", "computers",   "notebook",   800,   3,          0.06),
    ("zeta",    "computers",   "mouse",      30,    6,          0.15),
    (None,      None,          None,         20,    6,          0.15),   # 没有品牌、没有品类的配件
    ("zeta",    "electronics", "audio",      60,    5,          0.12),
]


def make_dims() -> pd.DataFrame:
    rows = [{"product_id": i + 1, "brand": b, "level1": l1, "level2": l2}
            for i, (b, l1, l2, *_rest) in enumerate(PRODUCTS)]
    return pd.DataFrame(rows)


def make_raw(seed: int = 7, visitors: int = 4000, day: date = DAY) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    pop = np.array([p[4] for p in PRODUCTS], dtype=float)
    pop /= pop.sum()
    hour_w = np.array([1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 7, 7, 6, 4, 2, 1], dtype=float)
    hour_w /= hour_w.sum()

    rows = []
    for k in range(7, -1, -1):
        d = day - timedelta(days=k)
        n_vis = int(visitors * rng.normal(1, 0.02))
        users = rng.choice(np.arange(1, 50_000) * 10, size=n_vis, replace=False)
        for u in users:
            is_new = int(rng.random() < 0.3)
            h = int(rng.choice(24, p=hour_w))
            for pid in rng.choice(len(PRODUCTS), size=rng.integers(1, 5), replace=False, p=pop):
                _, _, _, price, _, cvr = PRODUCTS[pid]
                rows.append((d, h, 0, u, pid + 1, float(price), is_new))
                if rng.random() < cvr:
                    rows.append((d, h, 2, u, pid + 1, float(price), is_new))
    return pd.DataFrame(rows, columns=["d", "h", "et", "user_id", "product_id", "price", "is_new"])


def make_frame(**kw) -> pd.DataFrame:
    return enrich(make_raw(**kw), make_dims(), kw.get("day", DAY))


# ---------------------------------------------------------------------------
# 在「当天」注入异动（和 scripts/build_scenarios.py 里的 SQL 注入是同一个意思）
# ---------------------------------------------------------------------------
def _today(frame, day=DAY):
    return frame["d"] == pd.Timestamp(day)


def drop_purchases(frame, mask, share, seed=1):
    """删掉满足 mask 的当天购买事件中的 share 比例。"""
    rng = np.random.default_rng(seed)
    target = _today(frame) & (frame["et"] == 2) & mask
    drop = target & (rng.random(len(frame)) < share)
    return frame[~drop].reset_index(drop=True)


def drop_users(frame, user_mask, share, seed=2, extra_mask=None):
    """删掉当天一部分用户的全部事件（流量下降）。extra_mask 可以只删这些用户在某个品类的事件。"""
    rng = np.random.default_rng(seed)
    users = frame.loc[_today(frame) & user_mask, "user_id"].unique()
    chosen = set(users[rng.random(len(users)) < share])
    drop = _today(frame) & frame["user_id"].isin(chosen)
    if extra_mask is not None:
        drop &= extra_mask
    return frame[~drop].reset_index(drop=True)


def change_price(frame, mask, factor):
    out = frame.copy()
    target = _today(out) & mask
    out.loc[target, "price"] = out.loc[target, "price"] * factor
    return out   # 价格段按基线参考价划分，降价不会改变商品所在的价格段


def duplicate_purchases(frame, mask, share, seed=3):
    rng = np.random.default_rng(seed)
    target = frame[_today(frame) & (frame["et"] == 2) & mask]
    extra = target[rng.random(len(target)) < share]
    return pd.concat([frame, extra], ignore_index=True)


def drop_hours(frame, hours):
    return frame[~(_today(frame) & frame["hour"].isin(hours))].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 用 pandas 模拟 eval/scenario_sql.py 的 SQL 注入（语义一一对应），用来离线测试整个评测流程
# ---------------------------------------------------------------------------
def apply_ops(frame, ops, day=DAY):
    """所有条件都在原始数据上判断（和 SQL 视图一样），再统一删除、改价、复制。"""
    today = _today(frame, day)
    drop = pd.Series(False, index=frame.index)
    dup = pd.Series(False, index=frame.index)
    new_price = frame["price"].copy()
    for op in ops:
        f = op.get("filter", {})
        m = today.copy()
        for dim in ("brand", "level1", "level2"):
            if dim in f:
                m &= frame[dim].astype(str) == f[dim]
        if f.get("new_user"):
            m &= frame["is_new"] == 1
        if "min_price" in f:
            m &= frame["price"] >= f["min_price"]
        if "hours" in f:
            m &= frame["hour"].isin(f["hours"])
        if op.get("scope", "all") == "purchase":
            m &= frame["et"] == 2
        k = int(round(op.get("share", 1.0) * 100))
        if op.get("unit", "event") == "event":
            sample = pd.Series(frame.index % 100 < k, index=frame.index)
        else:
            sample = (frame["user_id"] // 10) % 100 < k
        if op["op"] == "delete":
            drop |= m & sample
        elif op["op"] == "duplicate":
            dup |= m & sample
        elif op["op"] == "price":
            new_price = new_price.where(~m, (frame["price"] * op["factor"]).round(2))
    out = frame.assign(price=new_price)
    out = pd.concat([out[~drop], out[dup & ~drop]], ignore_index=True)
    return out

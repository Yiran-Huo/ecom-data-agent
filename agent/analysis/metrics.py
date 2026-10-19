"""
基础指标计算（纯函数，只依赖 pandas）。

核心是 daily_table：把事件表汇总成「每天 ×（某个维度的每个元素）」的四个基础量
    uv      浏览用户数
    buyers  有效购买的用户数
    n       有效购买次数
    gmv     有效购买金额
其余指标都由这四个量算出来：CVR = buyers / uv，F = n / buyers，P = gmv / n。

compare 再把它变成「当天 vs 基线」的对比表，是异常检测、拆解、下钻的共同输入。
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from agent.analysis import config as C

BASE_COLS = ["uv", "buyers", "n", "gmv"]


def apply_filters(frame: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """按过滤条件取子集，例如 {"level1": "electronics"}。filters 需要先经过 data.normalize_filters。"""
    if not filters:
        return frame
    mask = pd.Series(True, index=frame.index)
    for dim, value in filters.items():
        if dim == "hour":
            mask &= frame["hour"] == int(value)
        else:
            col = frame[dim].astype(str)
            mask &= (col == value) | (col.str.lower() == str(value).lower())
    return frame[mask]


def exclude(frame: pd.DataFrame, conditions: list[dict]) -> pd.DataFrame:
    """去掉满足任一条件的事件。每个条件是一个 dict，dict 内部是「且」，条件之间是「或」。"""
    if not conditions:
        return frame
    drop = pd.Series(False, index=frame.index)
    for cond in conditions:
        drop |= frame.index.isin(apply_filters(frame, cond).index)
    return frame[~drop]


def window_days(day: date) -> list[pd.Timestamp]:
    """基线 7 天 + 当天，共 8 个日期。"""
    return [pd.Timestamp(day - timedelta(days=k)) for k in range(C.BASELINE_DAYS, -1, -1)]


def daily_table(frame: pd.DataFrame, day: date, dim: str | None = None) -> pd.DataFrame:
    """
    返回以 (d) 或 (d, dim) 为索引、列为 uv / buyers / n / gmv 的表。
    某个元素某天没有数据时补 0（例如某品牌某天没有成交），否则基线均值会算错。
    """
    keys = ["d"] + ([dim] if dim else [])
    views = frame[frame["et"] == 0]
    buys = frame[(frame["et"] == 2) & (frame["price"] > 0)]
    table = pd.concat({
        "uv": views.groupby(keys, observed=True)["user_id"].nunique(),
        "buyers": buys.groupby(keys, observed=True)["user_id"].nunique(),
        "n": buys.groupby(keys, observed=True).size(),
        "gmv": buys.groupby(keys, observed=True)["price"].sum(),
    }, axis=1).fillna(0)

    days = window_days(day)
    if dim:
        elements = table.index.get_level_values(dim).unique()
        full = pd.MultiIndex.from_product([days, elements], names=["d", dim])
    else:
        full = pd.Index(days, name="d")
    return table.reindex(full, fill_value=0)


def compare(daily: pd.DataFrame, day: date, baseline: str = "prev7") -> pd.DataFrame:
    """
    当天 vs 基线。返回的列：cur_uv, cur_buyers, cur_n, cur_gmv, base_uv, base_buyers, base_n, base_gmv。
    按维度汇总时每行是一个元素；不按维度时只有一行（索引为 "全部"）。
    prev7：基线 = 前 7 天各基础量的日均值（日均 UV、日均购买用户……），四个因子由日均值推出来，
           这样基线的四个因子相乘正好等于基线 GMV，拆解时才能对上。
    wow：  基线 = 上周同一天。
    """
    if baseline not in C.BASELINES:
        raise ValueError(f"baseline 只能是 {', '.join(C.BASELINES)}")
    today = pd.Timestamp(day)
    days = window_days(day)
    has_dim = isinstance(daily.index, pd.MultiIndex)

    def pick(d_list):
        if has_dim:
            part = daily[daily.index.get_level_values("d").isin(d_list)]
            return part.groupby(level=1, observed=True).sum() / len(d_list)
        return daily.loc[d_list].sum().to_frame("全部").T / len(d_list)

    cur = pick([today])
    base = pick(days[:-1] if baseline == "prev7" else [days[0]])
    out = pd.concat([cur.add_prefix("cur_"), base.add_prefix("base_")], axis=1).fillna(0)
    return out


def factors(uv, buyers, n, gmv) -> dict:
    """四个基础量 → 四个因子。分母为 0 时得到 NaN。"""
    div = lambda a, b: a / b if b else np.nan
    return {"UV": uv, "CVR": div(buyers, uv), "F": div(n, buyers), "P": div(gmv, n)}


def row_factors(row: pd.Series, prefix: str) -> dict:
    return factors(row[f"{prefix}uv"], row[f"{prefix}buyers"], row[f"{prefix}n"], row[f"{prefix}gmv"])


def pct_change(cur, base) -> float:
    if base is None or pd.isna(base) or base == 0 or pd.isna(cur):
        return np.nan
    return cur / base - 1

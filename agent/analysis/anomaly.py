"""
SOP 第 1 步：确认异常（纯函数）。

两件事：
1. GMV 当天 vs 基线：变化幅度 + z 分数，两个都超过阈值才算异常
2. 数据完整性：按小时看事件数（浏览 + 加购 + 购买），某几个小时突然接近 0，
   更可能是数据缺失或系统故障，而不是业务变化。这种情况要先报告，不要急着找业务原因。
"""
from datetime import date

import numpy as np
import pandas as pd

from agent.analysis import config as C
from agent.analysis.metrics import compare, daily_table, window_days


def hourly_gaps(frame: pd.DataFrame, day: date) -> list[dict]:
    """找出当天事件数异常少的小时。返回 [{hour, cur, base}, ...]。"""
    days = window_days(day)
    counts = frame.groupby(["d", "hour"], observed=True).size().unstack(fill_value=0)
    counts = counts.reindex(index=days, columns=range(24), fill_value=0)
    base = counts.iloc[:-1].mean()
    cur = counts.iloc[-1]
    gaps = []
    for h in range(24):
        if base[h] >= C.HOUR_GAP_MIN_EVENTS and cur[h] < C.HOUR_GAP_RATIO * base[h]:
            gaps.append({"hour": h, "cur": int(cur[h]), "base": float(base[h])})
    return gaps


def detect(frame: pd.DataFrame, day: date, baseline: str = "prev7") -> dict:
    """
    返回：
        cur, base            当天 GMV、基线 GMV
        change, rel          差值、相对变化
        z                    (当天 − 前 7 天均值) ÷ 前 7 天标准差
        history              前 7 天每天的 GMV（给模型看，判断基线本身稳不稳）
        is_anomaly, direction
        hour_gaps            数据完整性检查的结果
    """
    daily = daily_table(frame, day)
    row = compare(daily, day, baseline).iloc[0]
    cur, base = float(row["cur_gmv"]), float(row["base_gmv"])

    history = daily["gmv"].iloc[:-1]
    mean, std = float(history.mean()), float(history.std(ddof=1))
    z = (cur - mean) / std if std > 0 else np.nan
    rel = cur / base - 1 if base else np.nan

    is_anomaly = bool(abs(rel) >= C.REL_THRESHOLD and (np.isnan(z) or abs(z) >= C.Z_THRESHOLD))
    return {
        "date": day.isoformat(),
        "baseline": baseline,
        "cur": cur,
        "base": base,
        "change": cur - base,
        "rel": rel,
        "z": z,
        "history": {d.strftime("%m-%d"): float(v) for d, v in history.items()},
        "is_anomaly": is_anomaly,
        "direction": "down" if cur < base else "up",
        "hour_gaps": hourly_gaps(frame, day),
    }

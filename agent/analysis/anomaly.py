"""
SOP 第 1 步：确认异常（纯函数）。

三件事：
1. GMV 当天 vs 基线：变化幅度 + z 分数，两个都超过阈值才算异常
2. 数据完整性：
   - 按小时：某几个小时浏览、加购、购买同时接近 0 → 疑似系统故障或数据管道缺失
   - 按事件类型：某一类事件（例如购买）整天接近 0，其他事件正常 → 疑似这类事件的埋点丢失
     真实例子：原始数据 2019-11-15 有 574 万条浏览、48 万条加购，但购买是 0 条
   这两种情况都要先作为数据问题报告，不要急着找业务原因
3. 基线是否可信：前 7 天里有极端的日子（例如 GMV 为 0，或者是平时的好几倍），均值和标准差都会被拉偏。
   baseline_skip_days 找出这些日子，prev7 基线和 z 分数都跳过它们（所有工具共用同一组，数字才对得上）
"""
from datetime import date

import numpy as np
import pandas as pd

from agent.analysis import config as C
from agent.analysis.metrics import compare, daily_table, window_days

EVENT_NAMES = {0: "浏览", 1: "加购", 2: "购买"}


def _base_rows(days, skip_days):
    skip = {pd.Timestamp(d) for d in skip_days}
    return [d for d in days[:-1] if d not in skip]


def hourly_gaps(frame: pd.DataFrame, day: date, skip_days=()) -> list[dict]:
    """找出当天事件数异常少的小时。返回 [{hour, cur, base}, ...]。基线同样跳过极端日。"""
    days = window_days(day)
    counts = frame.groupby(["d", "hour"], observed=True).size().unstack(fill_value=0)
    counts = counts.reindex(index=days, columns=range(24), fill_value=0)
    base = counts.loc[_base_rows(days, skip_days)].mean()
    cur = counts.iloc[-1]
    gaps = []
    for h in range(24):
        if base[h] >= C.HOUR_GAP_MIN_EVENTS and cur[h] < C.HOUR_GAP_RATIO * base[h]:
            gaps.append({"hour": h, "cur": int(cur[h]), "base": float(base[h])})
    return gaps


def event_gaps(frame: pd.DataFrame, day: date, skip_days=()) -> list[dict]:
    """找出当天整体缺失的事件类型（浏览 / 加购 / 购买）。返回 [{event, cur, base}, ...]。"""
    days = window_days(day)
    counts = frame.groupby(["d", "et"], observed=True).size().unstack(fill_value=0)
    counts = counts.reindex(index=days, columns=list(EVENT_NAMES), fill_value=0)
    base = counts.loc[_base_rows(days, skip_days)].mean()
    cur = counts.iloc[-1]
    gaps = []
    for et, name in EVENT_NAMES.items():
        if base[et] >= C.EVENT_GAP_MIN_EVENTS and cur[et] < C.HOUR_GAP_RATIO * base[et]:
            gaps.append({"event": name, "cur": int(cur[et]), "base": float(base[et])})
    return gaps


def unstable_days(history: pd.Series) -> list[dict]:
    """基线里的极端日子：GMV 低于中位数的 30%，或高于中位数的 2.5 倍。"""
    med = history.median()
    if med <= 0:
        return []
    return [{"date": d.strftime("%Y-%m-%d"), "gmv": float(v), "ratio": float(v / med)}
            for d, v in history.items()
            if v < C.BASELINE_LOW_RATIO * med or v > C.BASELINE_HIGH_RATIO * med]


def baseline_skip_days(frame: pd.DataFrame, day: date) -> list[str]:
    """全站 GMV 的前 7 天里需要跳过的极端日。下钻到子集时也用这一组（按全站判断），保证各工具口径一致。"""
    history = daily_table(frame, day)["gmv"].iloc[:-1]
    bad = unstable_days(history)
    if len(history) - len(bad) < 3:   # 剩下不到 3 天就不跳过了
        return []
    return [u["date"] for u in bad]


def detect(frame: pd.DataFrame, day: date, baseline: str = "prev7", skip_days=()) -> dict:
    """
    返回：
        cur, base            当天 GMV、基线 GMV
        change, rel          差值、相对变化
        z                    (当天 − 前 7 天均值) ÷ 前 7 天标准差
        history              前 7 天每天的 GMV（给模型看，判断基线本身稳不稳）
        is_anomaly, direction
        hour_gaps            按小时的数据完整性（事件明显偏少的小时）
        hour_gap_share       这些小时缺失的事件合计占全天基线事件的比例；≥ HOUR_GAP_MIN_SHARE 才算数据问题
        event_gaps           按事件类型的数据完整性
        unstable_days        这个范围内基线里的极端日子
        skipped_days         计算基线和 z 时跳过的日子（由调用方传入，通常是全站的极端日）
    """
    daily = daily_table(frame, day)
    row = compare(daily, day, baseline, skip_days).iloc[0]
    cur, base = float(row["cur_gmv"]), float(row["base_gmv"])

    history = daily["gmv"].iloc[:-1]
    skip = {pd.Timestamp(d) for d in skip_days}
    clean = history[~history.index.isin(skip)]
    mean, std = float(clean.mean()), float(clean.std(ddof=1))
    z = (cur - mean) / std if std > 0 else np.nan
    rel = cur / base - 1 if base else np.nan

    is_anomaly = bool(abs(rel) >= C.REL_THRESHOLD and (np.isnan(z) or abs(z) >= C.Z_THRESHOLD))

    gaps = hourly_gaps(frame, day, skip_days)
    days = window_days(day)
    daily_events = frame.groupby("d").size().reindex(days, fill_value=0)
    base_events = float(daily_events.loc[_base_rows(days, skip_days)].mean())
    gap_share = sum(g["base"] - g["cur"] for g in gaps) / base_events if base_events else 0.0
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
        "hour_gaps": gaps,
        "hour_gap_share": gap_share,
        "event_gaps": event_gaps(frame, day, skip_days),
        "unstable_days": unstable_days(history),
        "skipped_days": sorted(str(d) for d in skip_days),
    }

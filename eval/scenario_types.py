"""
场景类型：每种类型 = 一组注入操作 + 一份标准答案（纯函数，给一个真实数据的 frame 就能算，不连数据库）。

场景定义写在 eval/scenarios/specs.json，例如：
    {"id": "d01", "split": "dev", "type": "brand_purchase_drop", "date": "2019-10-16",
     "target": {"dim": "brand", "rank": 1}, "strength": 0.5}
意思是：在 10-16 当天，删掉「基线 7 天 GMV 排名第 1 的品牌」50% 的购买。
目标用排名而不是写死品牌名，这样换日期、换数据后场景仍然成立；具体是哪个品牌由 resolve() 算出来。

| 类型                  | 注入                                   | 标准答案                    |
| brand_purchase_drop   | 删掉某品牌当天一部分购买               | brand=X，元素内 CVR / F 下降 |
| level2_purchase_drop  | 删掉某二级品类当天一部分购买           | level2=W                     |
| category_traffic_drop | 某一级品类的一部分访客当天在该品类的行为全部消失 | level1=Y，UV 下降  |
| brand_price_cut       | 某品牌当天价格 × (1 − 强度)            | brand=X，P 下降              |
| site_traffic_drop     | 当天一部分用户的全部行为消失           | 全面性变化（broad），UV 下降 |
| new_user_drop         | 当天一部分新用户的全部行为消失         | user_type=new，UV 下降       |
| hour_outage           | 当天某几个小时的全部事件消失           | 数据问题（data_issue）       |
| brand_promo_spike     | 某品牌当天一部分购买多复制一份         | brand=Z，F 上涨              |
| high_price_mix        | 删掉当天一部分 ≥ 1000 的购买           | price_band=1000+（结构变化） |
| two_causes            | 两种注入叠加                           | 两个原因都要找到             |
| no_anomaly            | 不注入                                 | 正常波动                     |
| natural               | 不注入，用真实数据里本来就有的异常日   | 写在 spec 的 expected 里     |

natural 例子：2019-11-15 原始数据里购买事件为 0（浏览、加购正常），标准答案是数据问题（data_issue）。
"""
import json
from datetime import date

import pandas as pd

from agent.analysis import config as C

QUESTION = "请排查 {date} 的 GMV 是否有异常；如果有，定位原因。"


def ranked_elements(frame: pd.DataFrame, day: date, dim: str, by: str = "gmv") -> list:
    """基线 7 天里，某维度各元素按 GMV（或浏览用户数）从大到小排序，去掉空值。"""
    base = frame[frame["d"] < pd.Timestamp(day)]
    if by == "uv":
        s = base[base["et"] == 0].groupby(dim, observed=True)["user_id"].nunique()
    else:
        buys = base[(base["et"] == 2) & (base["price"] > 0)]
        s = buys.groupby(dim, observed=True)["price"].sum()
    s = s.sort_values(ascending=False)
    null = C.NULL_LABELS.get(dim)
    return [str(v) for v in s.index if str(v) != null]


def _target(spec: dict, frame, day) -> str:
    t = spec["target"]
    elements = ranked_elements(frame, day, t["dim"], t.get("by", "gmv"))
    if t["rank"] > len(elements):
        raise ValueError(f"{t['dim']} 只有 {len(elements)} 个元素，没有第 {t['rank']} 名")
    return elements[t["rank"] - 1]


def _one(spec: dict, frame, day) -> dict:
    """解析单一类型。返回 ops、causes、pattern、direction、main_factor_accept、targets。"""
    t, s = spec["type"], spec.get("strength", 1.0)
    down = {"direction": "down", "is_anomaly": True}

    if t in ("brand_purchase_drop", "level2_purchase_drop"):
        dim = "brand" if t == "brand_purchase_drop" else "level2"
        x = _target(spec, frame, day)
        return {**down, "pattern": "concentrated", "main_factor_accept": [], "targets": {dim: x},
                "ops": [{"op": "delete", "filter": {dim: x}, "scope": "purchase", "share": s, "unit": "event"}],
                "causes": [{"dimension": dim, "value": x, "factor_accept": ["CVR", "F"]}]}
    if t == "category_traffic_drop":
        y = _target(spec, frame, day)
        return {**down, "pattern": "concentrated", "main_factor_accept": [], "targets": {"level1": y},
                "ops": [{"op": "delete", "filter": {"level1": y}, "scope": "all", "share": s, "unit": "user"}],
                "causes": [{"dimension": "level1", "value": y, "factor_accept": ["UV"]}]}
    if t == "brand_price_cut":
        x = _target(spec, frame, day)
        return {**down, "pattern": "concentrated", "main_factor_accept": ["P"], "targets": {"brand": x},
                "ops": [{"op": "price", "filter": {"brand": x}, "factor": round(1 - s, 4)}],
                "causes": [{"dimension": "brand", "value": x, "factor_accept": ["P"]}]}
    if t == "site_traffic_drop":
        return {**down, "pattern": "broad", "main_factor_accept": ["UV"], "targets": {},
                "ops": [{"op": "delete", "filter": {}, "scope": "all", "share": s, "unit": "user"}],
                "causes": []}
    if t == "new_user_drop":
        return {**down, "pattern": "concentrated", "main_factor_accept": ["UV"], "targets": {"user_type": "new"},
                "ops": [{"op": "delete", "filter": {"new_user": True}, "scope": "all", "share": s, "unit": "user"}],
                "causes": [{"dimension": "user_type", "value": "new", "factor_accept": ["UV"]}]}
    if t == "hour_outage":
        hours = spec["hours"]
        return {**down, "pattern": "data_issue", "main_factor_accept": [], "targets": {"hours": hours},
                "ops": [{"op": "delete", "filter": {"hours": hours}, "scope": "all", "share": 1.0}],
                "causes": [{"dimension": "hour", "value": str(h), "factor_accept": []} for h in hours]}
    if t == "brand_promo_spike":
        z = _target(spec, frame, day)
        return {"direction": "up", "is_anomaly": True, "pattern": "concentrated", "main_factor_accept": [],
                "targets": {"brand": z},
                "ops": [{"op": "duplicate", "filter": {"brand": z}, "scope": "purchase", "share": s, "unit": "event"}],
                "causes": [{"dimension": "brand", "value": z, "factor_accept": ["F"]}]}
    if t == "high_price_mix":
        band = C.PRICE_BANDS[-1]
        return {**down, "pattern": "concentrated", "main_factor_accept": [], "targets": {"price_band": band[2]},
                "ops": [{"op": "delete", "filter": {"min_price": band[0]}, "scope": "purchase", "share": s,
                         "unit": "event"}],
                "causes": [{"dimension": "price_band", "value": band[2], "factor_accept": ["CVR", "F"]}]}
    if t == "natural":
        e = spec["expected"]
        return {"direction": e.get("direction", ""), "is_anomaly": e["is_anomaly"], "pattern": e["pattern"],
                "main_factor_accept": e.get("main_factor_accept", []), "targets": {}, "ops": [],
                "causes": e.get("causes", [])}
    if t == "no_anomaly":
        return {"direction": "", "is_anomaly": False, "pattern": "normal", "main_factor_accept": [],
                "targets": {}, "ops": [], "causes": []}
    raise ValueError(f"未知的场景类型 {t}")


def zeroed_segments(ops: list[dict], pattern: str) -> list[str]:
    """
    业务类场景里，把某个分段的购买（或某类用户的全部行为）删到 0 的操作。
    这种数据和「这个分段的埋点丢了」分不开：浏览、加购都正常，购买正好是 0 —— 真实业务下滑很少会正好掉到 0。
    第一版 d02（删掉某品牌 100% 的购买）Agent 判成了数据问题，回头看它的判断是合理的，问题出在场景设计。
    所以除了 data_issue 类场景，删除比例必须 < 1。
    """
    if pattern == "data_issue":
        return []
    return [json.dumps(op["filter"], ensure_ascii=False) for op in ops
            if op["op"] == "delete" and op.get("filter") and op.get("share", 1.0) >= 1.0]


def resolve(spec: dict, frame: pd.DataFrame) -> dict:
    """场景定义 + 当天的真实数据 → 注入操作 + 标准答案。"""
    day = date.fromisoformat(spec["date"])
    if spec["type"] == "two_causes":
        parts = [_one(p, frame, day) for p in spec["parts"]]
        r = {"direction": "down", "is_anomaly": True, "pattern": "concentrated", "main_factor_accept": [],
             "targets": {f"part{i + 1}": p["targets"] for i, p in enumerate(parts)},
             "ops": [op for p in parts for op in p["ops"]],
             "causes": [c for p in parts for c in p["causes"]]}
    else:
        r = _one(spec, frame, day)
    expected = {k: r[k] for k in ["is_anomaly", "direction", "pattern", "main_factor_accept", "causes"]}
    return {"ops": r["ops"], "targets": r["targets"], "expected": expected,
            "question": QUESTION.format(date=spec["date"])}

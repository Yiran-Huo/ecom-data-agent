"""
纯算法基线：不用大模型，把 SOP 五步按固定规则串起来，直接给出结构化结论。

它有两个用途：
1. 评测里的对照组。如果纯算法已经能答对大部分场景，大模型的价值就要体现在
   多原因、结构效应、数据问题判断、报告可读性这些规则不擅长的地方。
2. 场景构建时检查注入的异动「能不能被发现」（scripts/build_scenarios.py）。

结论的格式和 Agent 的 submit_conclusion 完全一样，评测可以用同一套判分。
"""
from datetime import date

import pandas as pd

from agent.analysis import config as C
from agent.analysis.anomaly import detect
from agent.analysis.attribution import rank_dimensions, total_lmdi, verify_exclusion
from agent.analysis.decompose import main_factors
from agent.analysis.metrics import apply_filters, exclude


def _drill(frame: pd.DataFrame, day: date, dim: str, value, baseline: str) -> list[dict]:
    """从一个元素继续往下钻：子维度里如果有单个元素解释了 ≥ 70% 的变化，就继续，否则停。"""
    path = [{"dimension": dim, "value": str(value)}]
    filters, used = {dim: value}, {dim}
    current = dim
    for _ in range(C.MAX_DEPTH - 1):
        children = [d for d in C.DRILL_CHILDREN.get(current, []) if d not in used]
        if not children:
            break
        sub = apply_filters(frame, filters)
        ranking = rank_dimensions(sub, day, baseline, dims=children)
        best = ranking[0] if ranking else None
        if not (best and best["concentrated"] and len(best["elements"]) == 1
                and best["explained"] >= C.EXPLAINED_ENOUGH):
            break
        value = best["elements"][0]["value"]
        path.append({"dimension": best["dim"], "value": str(value)})
        filters[best["dim"]] = value
        used.add(best["dim"])
        current = best["dim"]
    return path


def auto_diagnose(frame: pd.DataFrame, day: date, baseline: str = "prev7") -> dict:
    det = detect(frame, day, baseline)
    conclusion = {
        "date": day.isoformat(),
        "is_anomaly": det["is_anomaly"],
        "direction": det["direction"],
        "change_pct": round(det["rel"], 4),
        "main_factor": "",
        "pattern": "normal",
        "root_causes": [],
        "confidence": "high",
        "hypotheses": [],
    }

    # 1. 先排除数据问题
    if det["hour_gaps"]:
        conclusion.update(
            is_anomaly=True, pattern="data_issue", confidence="medium",
            root_causes=[{"dimension": "hour", "value": str(g["hour"]), "factor": "UV",
                          "contribution_share": None, "path": []} for g in det["hour_gaps"]],
            hypotheses=["这些小时的浏览、加购、购买同时接近 0，疑似埋点或数据管道缺失，需要查系统日志"],
        )
        return conclusion
    if not det["is_anomaly"]:
        return conclusion

    # 2. 拆因子
    dec = total_lmdi(frame, day, baseline)
    mains = main_factors(dec)
    conclusion["main_factor"] = mains[0] if mains else ""

    # 3. 定位维度；4. 剔除验证。剔除后仍然异常，说明还有别的原因，对剩下的部分再找一轮（最多 2 轮）
    causes, rest = [], frame
    for _ in range(2):
        ranking = rank_dimensions(rest, day, baseline)
        best = ranking[0]
        if not best["concentrated"]:
            break
        for el in best["elements"]:
            causes.append({
                "dimension": best["dim"],
                "value": str(el["value"]),
                "factor": el["main_factor"],
                "contribution_share": round(el["delta"] / det["change"], 4) if det["change"] else None,
                "path": _drill(rest, day, best["dim"], el["value"], baseline),
            })
        conditions = [{c["dimension"]: c["value"]} for c in causes]
        check = verify_exclusion(frame, day, conditions, baseline)
        if not check["residual_anomaly"]:
            break
        rest = exclude(frame, conditions)

    if not causes:
        conclusion.update(pattern="broad", confidence="medium",
                          hypotheses=["变化分布在所有维度上，没有集中的元素，可能是全站流量或外部因素"])
        return conclusion

    causes.sort(key=lambda c: -abs(c["contribution_share"] or 0))   # 贡献大的排前面
    conclusion.update(
        pattern="concentrated",
        root_causes=causes,
        confidence="high" if (not check["residual_anomaly"] and check["explained"] >= C.EXPLAINED_ENOUGH) else "medium",
    )
    return conclusion

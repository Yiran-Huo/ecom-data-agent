"""
结构化结论：Agent 最后通过 submit_conclusion 工具提交，评测脚本直接读取判分。

用工具调用交结论，而不是从文字里解析：参数有 JSON Schema 约束，字段缺了、取值不对，
这里会返回清楚的错误，模型改了再交，不会因为格式问题白跑一次。
"""
from agent.analysis import config as C

PATTERNS = {
    "normal": "正常波动，不算异常",
    "concentrated": "变化集中在少数几个元素上",
    "broad": "全面性变化，没有集中的元素",
    "data_issue": "疑似数据缺失或系统故障",
}

ITEM = {
    "type": "object",
    "properties": {
        "dimension": {"type": "string", "enum": C.DIMENSIONS},
        "value": {"type": "string", "description": "元素的取值，和工具输出里写的一样，例如 samsung、electronics、new、13"},
    },
    "required": ["dimension", "value"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "排查的日期 YYYY-MM-DD"},
        "is_anomaly": {"type": "boolean"},
        "direction": {"type": "string", "enum": ["up", "down"]},
        "change_pct": {"type": "number", "description": "GMV 相对基线的变化，小数，例如 -0.21"},
        "main_factor": {"type": "string", "enum": C.FACTORS + [""],
                        "description": "全站层面的主导因子；正常波动时写空字符串"},
        "pattern": {"type": "string", "enum": list(PATTERNS),
                    "description": "; ".join(f"{k} = {v}" for k, v in PATTERNS.items())},
        "root_causes": {
            "type": "array",
            "description": "定位到的原因，贡献大的在前。pattern 是 normal 或 broad 时为空列表",
            "items": {
                "type": "object",
                "properties": {
                    **ITEM["properties"],
                    "factor": {"type": "string", "enum": C.FACTORS, "description": "这个元素内部是哪个因子在变"},
                    "contribution_share": {"type": "number", "description": "该元素的 ΔGMV ÷ 全站 ΔGMV，例如 0.74"},
                    "path": {"type": "array", "items": ITEM,
                             "description": "下钻路径，例如 [{level1: electronics}, {level2: smartphone}, {brand: samsung}]"},
                },
                "required": ["dimension", "value", "factor"],
            },
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "hypotheses": {"type": "array", "items": {"type": "string"},
                       "description": "可能的业务解释，以及需要业务方确认的问题（数据本身无法证明的部分）"},
    },
    "required": ["date", "is_anomaly", "direction", "pattern", "root_causes", "confidence"],
}


def validate(c: dict) -> tuple[dict | None, list[str]]:
    """检查并规范化结论。返回 (规范化后的结论, 错误列表)；有错误时结论为 None。"""
    errors = []
    if not isinstance(c, dict):
        return None, ["结论必须是一个 JSON 对象"]
    for key in SCHEMA["required"]:
        if key not in c:
            errors.append(f"缺少字段 {key}")
    if errors:
        return None, errors

    out = {
        "date": str(c["date"]),
        "is_anomaly": bool(c["is_anomaly"]),
        "direction": str(c.get("direction", "")).lower(),
        "change_pct": c.get("change_pct"),
        "main_factor": str(c.get("main_factor") or "").upper(),
        "pattern": str(c["pattern"]).lower(),
        "root_causes": [],
        "confidence": str(c["confidence"]).lower(),
        "hypotheses": [str(h) for h in (c.get("hypotheses") or [])],
    }
    if out["direction"] not in ("up", "down"):
        errors.append("direction 只能是 up 或 down")
    if out["main_factor"] not in C.FACTORS + [""]:
        errors.append(f"main_factor 只能是 {', '.join(C.FACTORS)} 或空字符串")
    if out["pattern"] not in PATTERNS:
        errors.append(f"pattern 只能是 {', '.join(PATTERNS)}")
    if out["confidence"] not in ("high", "medium", "low"):
        errors.append("confidence 只能是 high、medium、low")

    causes = c.get("root_causes") or []
    if not isinstance(causes, list):
        errors.append("root_causes 必须是列表")
        causes = []
    for i, rc in enumerate(causes):
        if not isinstance(rc, dict) or rc.get("dimension") not in C.DIMENSIONS or "value" not in rc:
            errors.append(f"root_causes[{i}] 需要 dimension（{', '.join(C.DIMENSIONS)} 之一）和 value")
            continue
        factor = str(rc.get("factor", "")).upper()
        if factor not in C.FACTORS:
            errors.append(f"root_causes[{i}].factor 只能是 {', '.join(C.FACTORS)}")
        share = rc.get("contribution_share")
        try:
            share = float(share) if share is not None else None
        except (TypeError, ValueError):
            errors.append(f"root_causes[{i}].contribution_share 要写小数，例如 0.74")
            share = None
        path = [{"dimension": p.get("dimension"), "value": str(p.get("value"))}
                for p in (rc.get("path") or []) if isinstance(p, dict)]
        out["root_causes"].append({"dimension": rc["dimension"], "value": str(rc["value"]),
                                   "factor": factor, "contribution_share": share, "path": path})

    if out["pattern"] == "concentrated" and not out["root_causes"]:
        errors.append("pattern 是 concentrated 时，root_causes 不能为空")
    if out["pattern"] in ("normal", "broad") and out["root_causes"]:
        errors.append(f"pattern 是 {out['pattern']} 时，root_causes 应该为空（不要硬挑元素）")
    if out["pattern"] == "normal" and out["is_anomaly"]:
        errors.append("pattern 是 normal 时，is_anomaly 应该是 false")
    return (None, errors) if errors else (out, [])

"""
归因评测的判分规则（纯函数，执行计划 7.3）。

每个场景的标准答案 expected（由 scripts/build_scenarios.py 生成）：
    is_anomaly, direction, pattern
    main_factor_accept   全站主导因子可以接受的取值；空列表 = 这个场景不判主导因子（例如删掉某品牌的购买，
                         全站是 CVR 还是 P 先变，取决于这个品牌比平均贵还是便宜）
    causes               [{dimension, value, factor_accept, true_share}]：注入的原因，以及它真实解释了多少 ΔGMV

指标：
    top1          主指标。concentrated 场景：第一个原因（或它的下钻路径上的任一层）命中标准答案；
                  其他场景：pattern 判对（normal / broad / data_issue），且 normal、broad 没有硬挑元素
    anomaly_ok    是否异常、方向都对
    pattern_ok    pattern 判对
    false_alarm   normal / broad 场景里给出了具体元素
    factor_ok     全站主导因子对不对（只在 main_factor_accept 非空时判）
    cause_factor_ok  命中的原因，元素内部因子对不对
    recall        多原因场景：找到了几个真实原因
    share_ok      命中原因的贡献占比估计，和真实值相差 ≤ 15 个百分点
"""
SHARE_TOL = 0.15


def _norm(v) -> str:
    return str(v).strip().lower()


def _matches(pred_cause: dict, exp_cause: dict) -> bool:
    """原因本身或它的下钻路径上任意一层，和标准答案的 (维度, 取值) 一致就算命中。"""
    key = (exp_cause["dimension"], _norm(exp_cause["value"]))
    items = [pred_cause] + list(pred_cause.get("path") or [])
    return any((it.get("dimension"), _norm(it.get("value"))) == key for it in items)


def score(pred: dict | None, expected: dict) -> dict:
    blank = {"has_conclusion": False, "top1": False, "anomaly_ok": False, "pattern_ok": False,
             "false_alarm": None, "factor_ok": None, "cause_factor_ok": None, "recall": None, "share_ok": None}
    if expected["pattern"] in ("normal", "broad"):
        blank["false_alarm"] = False
    if not pred:
        return blank

    out = dict(blank, has_conclusion=True)
    exp_pattern = expected["pattern"]
    out["anomaly_ok"] = (pred["is_anomaly"] == expected["is_anomaly"]
                         and (not expected["is_anomaly"] or pred["direction"] == expected["direction"]))
    out["pattern_ok"] = pred["pattern"] == exp_pattern
    causes = pred.get("root_causes") or []

    if exp_pattern in ("normal", "broad"):
        out["false_alarm"] = bool(causes) or pred["pattern"] == "concentrated"
        out["top1"] = out["pattern_ok"] and not out["false_alarm"]
    elif exp_pattern == "data_issue":
        out["top1"] = out["pattern_ok"]
    else:   # concentrated
        exp_causes = expected["causes"]
        first = causes[0] if causes else None
        hit = next((e for e in exp_causes if first and _matches(first, e)), None)
        out["top1"] = hit is not None
        if hit:
            out["cause_factor_ok"] = first.get("factor") in hit["factor_accept"] if hit["factor_accept"] else None
            share = first.get("contribution_share")
            if share is not None and hit.get("true_share") is not None:
                out["share_ok"] = abs(share - hit["true_share"]) <= SHARE_TOL
        if len(exp_causes) > 1:
            found = sum(any(_matches(c, e) for c in causes) for e in exp_causes)
            out["recall"] = found / len(exp_causes)

    if expected.get("main_factor_accept"):
        out["factor_ok"] = pred.get("main_factor") in expected["main_factor_accept"]
    return out


def summarize(rows: list[dict]) -> dict:
    """把很多个 score() 的结果汇总成比例。None 表示这一项对这个场景不适用，不计入分母。"""
    def rate(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(bool(v) for v in vals) / len(vals) if vals else None

    def mean(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "n": len(rows),
        "top1": rate("top1"),
        "anomaly_ok": rate("anomaly_ok"),
        "pattern_ok": rate("pattern_ok"),
        "false_alarm": rate("false_alarm"),
        "factor_ok": rate("factor_ok"),
        "cause_factor_ok": rate("cause_factor_ok"),
        "recall": mean("recall"),
        "share_ok": rate("share_ok"),
        "no_conclusion": sum(not r["has_conclusion"] for r in rows),
    }

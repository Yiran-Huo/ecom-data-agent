"""
评测流程的离线测试：场景定义 → 解析目标 → 注入（用 pandas 模拟 SQL）→ 纯算法排查 → 判分。
不需要数据库。真实场景的强度在 eval/scenarios/specs.json，这里用适合合成数据的强度。
"""
import re
from datetime import date

import pytest

from agent.analysis.pipeline import auto_diagnose
from eval.scenario_sql import compile_ops, view_statements
from eval.scenario_types import ranked_elements, resolve
from eval.scoring import score, summarize
from synthetic import DAY, apply_ops, make_frame

D = DAY.isoformat()
B = lambda r: {"dim": "brand", "rank": r}   # noqa: E731

SPECS = [
    {"id": "x01", "type": "brand_purchase_drop", "target": B(1), "strength": 0.7},
    {"id": "x02", "type": "category_traffic_drop", "target": {"dim": "level1", "rank": 1, "by": "uv"}, "strength": 0.6},
    {"id": "x03", "type": "brand_price_cut", "target": B(1), "strength": 0.4},
    {"id": "x04", "type": "site_traffic_drop", "strength": 0.3},
    {"id": "x05", "type": "new_user_drop", "strength": 0.8},
    {"id": "x06", "type": "hour_outage", "hours": [10, 11, 12]},
    {"id": "x07", "type": "brand_promo_spike", "target": B(3), "strength": 1.0},
    {"id": "x08", "type": "no_anomaly"},
    {"id": "x09", "type": "level2_purchase_drop", "target": {"dim": "level2", "rank": 1}, "strength": 0.7},
    {"id": "x10", "type": "two_causes", "parts": [{"type": "brand_purchase_drop", "target": B(1), "strength": 0.6},
                                                  {"type": "brand_price_cut", "target": B(2), "strength": 0.7}]},
]


@pytest.fixture(scope="module")
def base():
    return make_frame()


def test_ranked_elements(base):
    brands = ranked_elements(base, DAY, "brand")
    assert brands[0] == "alpha" and "(无品牌)" not in brands


@pytest.mark.parametrize("spec", SPECS, ids=[s["type"] for s in SPECS])
def test_algorithm_solves_synthetic_scenarios(base, spec):
    spec = {**spec, "date": D, "split": "dev"}
    r = resolve(spec, base)
    frame = apply_ops(base, r["ops"])
    pred = auto_diagnose(frame, DAY)
    s = score(pred, r["expected"])
    assert s["top1"], (pred, r["expected"])
    assert s["anomaly_ok"]


def test_natural_data_issue_case(base):
    """真实案例（11-15 购买埋点缺失）：不注入，标准答案写在 spec 里。"""
    spec = {"id": "r01", "split": "real", "type": "natural", "date": D,
            "expected": {"is_anomaly": True, "direction": "down", "pattern": "data_issue"}}
    r = resolve(spec, base)
    assert r["ops"] == []
    frame = base[~((base["d"] == base["d"].max()) & (base["et"] == 2))].reset_index(drop=True)
    assert score(auto_diagnose(frame, DAY), r["expected"])["top1"]


def test_high_price_mix_resolves(base):
    """合成数据里高价商品太少，注入后不够异常（真实数据上由 build_scenarios.py 检查是否成立），这里只检查解析结果。"""
    r = resolve({"id": "x11", "type": "high_price_mix", "strength": 0.7, "date": D, "split": "dev"}, base)
    assert r["ops"] == [{"op": "delete", "filter": {"min_price": 1000}, "scope": "purchase", "share": 0.7,
                         "unit": "event"}]
    assert r["expected"]["causes"][0]["dimension"] == "price_band"


def test_scoring_rules():
    exp = {"is_anomaly": True, "direction": "down", "pattern": "concentrated", "main_factor_accept": ["P"],
           "causes": [{"dimension": "brand", "value": "apple", "factor_accept": ["P"], "true_share": 0.8},
                      {"dimension": "brand", "value": "xiaomi", "factor_accept": ["CVR"], "true_share": 0.3}]}
    pred = {"is_anomaly": True, "direction": "down", "pattern": "concentrated", "main_factor": "P",
            "root_causes": [{"dimension": "level2", "value": "smartphone", "factor": "P", "contribution_share": 0.7,
                             "path": [{"dimension": "level2", "value": "smartphone"},
                                      {"dimension": "brand", "value": "Apple"}]}]}
    s = score(pred, exp)
    assert s["top1"] and s["cause_factor_ok"] and s["share_ok"] and s["factor_ok"] and s["recall"] == 0.5

    broad = {"is_anomaly": True, "direction": "down", "pattern": "broad", "main_factor_accept": [], "causes": []}
    s = score(pred, broad)
    assert s["false_alarm"] and not s["top1"]
    wrong_pattern = {**pred, "pattern": "data_issue"}            # 原因对，但判成了数据问题
    assert not score(wrong_pattern, exp)["top1"]
    assert score(None, broad)["false_alarm"] is False and not score(None, broad)["has_conclusion"]

    issue = {"is_anomaly": True, "direction": "down", "pattern": "data_issue", "main_factor_accept": [], "causes": []}
    calm = {**pred, "is_anomaly": False, "pattern": "data_issue", "root_causes": []}
    assert score(calm, issue)["anomaly_ok"] and score(calm, issue)["top1"]   # 数据问题：只看 pattern

    summary = summarize([score(pred, exp), score(None, exp)])
    assert summary["top1"] == 0.5 and summary["no_conclusion"] == 1


def test_compile_sql():
    day = date(2019, 10, 16)
    ops = [{"op": "delete", "filter": {"brand": "o'neil"}, "scope": "purchase", "share": 0.5, "unit": "event"},
           {"op": "delete", "filter": {"new_user": True}, "scope": "all", "share": 0.3, "unit": "user"},
           {"op": "price", "filter": {"level2": "smartphone"}, "factor": 0.7},
           {"op": "duplicate", "filter": {"brand": "apple"}, "scope": "purchase", "share": 1.0}]
    sql = compile_ops(ops, day)
    assert "p.brand <=> 'o''neil'" in sql["delete"] and "MOD(e.event_id, 100) < 50" in sql["delete"]
    # 每个删除条件都包了 COALESCE：品牌为空时不会因为 NULL 被误删（第一版的 bug）
    assert sql["delete"].count("COALESCE((") == 2 and " = 'o''neil'" not in sql["delete"]
    assert "MOD(FLOOR(e.user_id / 10), 100) < 30" in sql["delete"] and "u.first_date = '2019-10-16'" in sql["delete"]
    assert sql["price"].startswith("CASE WHEN") and "e.price * 0.7" in sql["price"]
    assert "MOD" not in sql["duplicate"]   # 100% 复制不需要抽样条件
    assert "e.event_time >= '2019-10-16' AND e.event_time < '2019-10-17'" in sql["delete"]

    stmts = view_statements("ecom_s_d01", "ecom", ops, day)
    view = stmts[2]
    assert view.count("(") == view.count(")")
    assert re.search(r"FROM `ecom`.`events` e", view) and "WHERE NOT (" in view
    assert len(stmts) == 7

    from eval.scenario_sql import first_seen_statements, needs_first_seen
    assert needs_first_seen(ops)                                     # 有删除用户整天行为的操作
    assert not needs_first_seen([ops[0], ops[2], ops[3]])             # 只删购买、改价、复制：不需要重算
    fs = first_seen_statements("ecom_s_d06", "ecom", day)
    assert fs[0].startswith("DROP VIEW") and "first_date = '2019-10-16'" in fs[3] and fs[4].startswith("DELETE f")

    empty = compile_ops([], day)
    assert empty == {"delete": "FALSE", "price": "e.price", "duplicate": "FALSE"}


def test_zeroed_segments_rule():
    """业务类场景不能把分段删到 0（和埋点丢失分不开）；data_issue 类场景可以。"""
    from eval.scenario_types import zeroed_segments
    full = [{"op": "delete", "filter": {"brand": "samsung"}, "scope": "purchase", "share": 1.0}]
    part = [{"op": "delete", "filter": {"brand": "samsung"}, "scope": "purchase", "share": 0.8}]
    site = [{"op": "delete", "filter": {}, "scope": "all", "share": 1.0}]
    assert zeroed_segments(full, "concentrated") == ['{"brand": "samsung"}']
    assert zeroed_segments(part, "concentrated") == []
    assert zeroed_segments(full, "data_issue") == []
    assert zeroed_segments(site, "broad") == []


def test_specs_follow_zero_rule():
    import json
    from pathlib import Path
    specs = json.loads((Path(__file__).resolve().parent.parent / "eval/scenarios/specs.json").read_text())
    for s in [p for x in specs for p in x.get("parts", [x])]:
        if s["type"] in ("brand_purchase_drop", "level2_purchase_drop", "new_user_drop", "high_price_mix",
                         "category_traffic_drop"):
            assert s.get("strength", 1.0) < 1.0, s["id"]

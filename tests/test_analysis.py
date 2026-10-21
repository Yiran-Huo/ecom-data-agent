"""
分析工具层的单元测试：用合成数据（tests/synthetic.py），不需要数据库，也不调用大模型。

    pytest -q tests/test_analysis.py

测试分三类：
1. 数学性质：LMDI 四项之和 = ΔGMV；各元素 ΔGMV 之和 = ΔGMV
2. 每种异动注入后，工具能不能找到正确的原因
3. 输入检查：日期范围、过滤条件
"""
import math
from datetime import date

import pandas as pd
import pytest

from agent.analysis import config as C
from agent.analysis.anomaly import baseline_skip_days, detect
from agent.analysis.attribution import element_table, rank_dimensions, total_lmdi, verify_exclusion
from agent.analysis.data import normalize_filters, parse_day
from agent.analysis.decompose import lmdi, log_mean, main_factors
from agent.analysis.metrics import apply_filters
from agent.analysis.pipeline import auto_diagnose
from synthetic import (DAY, change_price, drop_hours, drop_purchases, drop_users, duplicate_purchases,
                       make_frame)


@pytest.fixture(scope="module")
def base():
    return make_frame()


# ---------------------------------------------------------------------------
# 1. 数学性质
# ---------------------------------------------------------------------------
def test_log_mean():
    assert log_mean(5, 5) == 5
    assert math.isclose(log_mean(2, 1), 1 / math.log(2))


def test_lmdi_contributions_sum_to_delta():
    base = {"uv": 1000, "buyers": 100, "n": 120, "gmv": 36000}
    cur = {"uv": 900, "buyers": 80, "n": 100, "gmv": 25000}
    res = lmdi(cur, base)
    total = sum(res["factors"][f]["contribution"] for f in C.FACTORS)
    assert res["ok"] and math.isclose(total, cur["gmv"] - base["gmv"], rel_tol=1e-9)


def test_lmdi_handles_zero():
    res = lmdi({"uv": 10, "buyers": 0, "n": 0, "gmv": 0}, {"uv": 10, "buyers": 1, "n": 1, "gmv": 5})
    assert not res["ok"] and main_factors(res) == []


def test_element_deltas_sum_to_total(base):
    frame = drop_purchases(base, base["brand"] == "alpha", 0.7)
    total = total_lmdi(frame, DAY)["delta"]
    for dim in C.DIMENSIONS:
        table = element_table(frame, DAY, dim)
        assert math.isclose(table["delta"].sum(), total, rel_tol=1e-6, abs_tol=1e-6), dim


def test_lmdi_on_real_frame_sums(base):
    frame = drop_users(base, base["user_id"] > 0, 0.3)
    res = total_lmdi(frame, DAY)
    total = sum(res["factors"][f]["contribution"] for f in C.FACTORS)
    assert math.isclose(total, res["delta"], rel_tol=1e-9)


# ---------------------------------------------------------------------------
# 2. 注入异动后能不能找到原因
# ---------------------------------------------------------------------------
def test_normal_day_is_not_anomaly(base):
    det = detect(base, DAY)
    assert not det["is_anomaly"] and det["hour_gaps"] == []
    assert auto_diagnose(base, DAY)["pattern"] == "normal"


def test_brand_purchase_drop(base):
    frame = drop_purchases(base, base["brand"] == "alpha", 0.7)
    det = detect(frame, DAY)
    assert det["is_anomaly"] and det["direction"] == "down"
    ranking = rank_dimensions(frame, DAY)
    assert ranking[0]["dim"] == "brand" and ranking[0]["elements"][0]["value"] == "alpha"
    concl = auto_diagnose(frame, DAY)
    assert concl["pattern"] == "concentrated"
    assert (concl["root_causes"][0]["dimension"], concl["root_causes"][0]["value"]) == ("brand", "alpha")
    assert concl["root_causes"][0]["factor"] in ("CVR", "F")


def test_verify_exclusion_explains_brand_drop(base):
    frame = drop_purchases(base, base["brand"] == "alpha", 0.7)
    check = verify_exclusion(frame, DAY, [{"brand": "alpha"}])
    assert check["before"]["is_anomaly"] and not check["residual_anomaly"]
    assert check["explained"] > 0.7


def test_broad_traffic_drop_has_no_single_cause(base):
    frame = drop_users(base, base["user_id"] > 0, 0.3)
    concl = auto_diagnose(frame, DAY)
    assert concl["is_anomaly"] and concl["pattern"] == "broad" and concl["root_causes"] == []
    assert concl["main_factor"] == "UV"


def test_hour_outage_is_data_issue(base):
    frame = drop_hours(base, [10, 11, 12])
    det = detect(frame, DAY)
    assert [g["hour"] for g in det["hour_gaps"]] == [10, 11, 12] and det["hour_gap_share"] >= C.HOUR_GAP_MIN_SHARE
    assert auto_diagnose(frame, DAY)["pattern"] == "data_issue"


def test_small_hour_gap_is_not_data_issue(base):
    """真实数据 11-06 0 点只有 80 条事件（平时约 1,100 条），只占全天约 1%：提示即可，不能当成数据问题。"""
    frame = drop_hours(base, [7])            # 合成数据里 7 点的事件约占全天 4%
    det = detect(frame, DAY)
    assert [g["hour"] for g in det["hour_gaps"]] == [7] and det["hour_gap_share"] < C.HOUR_GAP_MIN_SHARE
    frame = drop_purchases(frame, frame["brand"] == "alpha", 0.7)
    concl = auto_diagnose(frame, DAY)
    assert concl["pattern"] == "concentrated" and concl["root_causes"][0]["value"] == "alpha"
    assert any("轻微的数据缺口" in h for h in concl["hypotheses"])


def test_price_cut_points_to_brand_and_price_factor(base):
    frame = change_price(base, base["brand"] == "alpha", 0.6)
    concl = auto_diagnose(frame, DAY)
    assert concl["main_factor"] == "P"
    cause = concl["root_causes"][0]
    assert (cause["dimension"], cause["value"], cause["factor"]) == ("brand", "alpha", "P")


def test_price_band_uses_reference_price(base):
    """降价后商品仍然留在原来的价格段（按基线参考价分段）。"""
    frame = change_price(base, base["brand"] == "alpha", 0.6)
    from agent.analysis.data import enrich
    from synthetic import make_dims
    raw = frame[["d", "h", "et", "user_id", "product_id", "price", "is_new"]]
    again = enrich(raw, make_dims(), DAY)
    alpha_today = again[(again["brand"] == "alpha") & (again["d"] == again["d"].max())]
    assert set(alpha_today["price_band"].astype(str)) == {"500-1000"}


def test_promo_spike_is_up_and_frequency(base):
    frame = duplicate_purchases(base, base["brand"] == "beta", 1.0)
    concl = auto_diagnose(frame, DAY)
    assert concl["direction"] == "up" and concl["is_anomaly"]
    cause = concl["root_causes"][0]
    assert (cause["dimension"], cause["value"], cause["factor"]) == ("brand", "beta", "F")


def test_new_user_drop(base):
    frame = drop_users(base, base["user_type"] == "new", 0.7)
    concl = auto_diagnose(frame, DAY)
    cause = concl["root_causes"][0]
    assert (cause["dimension"], cause["value"], cause["factor"]) == ("user_type", "new", "UV")


def test_two_causes_found_in_two_rounds(base):
    frame = drop_purchases(base, base["brand"] == "alpha", 0.6)
    frame = change_price(frame, frame["brand"] == "epsilon", 0.3)
    concl = auto_diagnose(frame, DAY)
    found = {(c["dimension"], c["value"]) for c in concl["root_causes"]}
    assert {("brand", "alpha"), ("brand", "epsilon")} <= found


def test_drill_down_subset_uses_global_delta(base):
    frame = drop_purchases(base, base["level2"] == "smartphone", 0.6)
    total = total_lmdi(frame, DAY)["delta"]
    sub = apply_filters(frame, {"level1": "electronics"})
    table = element_table(sub, DAY, "brand", total_delta=total)
    assert table["ep"].sum() == pytest.approx(table["delta"].sum() / total)


def test_missing_purchase_events_is_data_issue(base):
    """真实数据 11-15：有浏览、有加购，购买 0 条 → 数据问题，而不是业务下跌。"""
    frame = base[~((base["d"] == base["d"].max()) & (base["et"] == 2))].reset_index(drop=True)
    det = detect(frame, DAY)
    assert [g["event"] for g in det["event_gaps"]] == ["购买"] and det["hour_gaps"] == []
    concl = auto_diagnose(frame, DAY)
    assert concl["pattern"] == "data_issue" and concl["root_causes"] == []


def _corrupt_baseline(frame, k=3):
    """把基线里的一天（DAY - k）的购买全部删掉，模拟 11-15 那样的极端日。"""
    bad = pd.Timestamp(DAY) - pd.Timedelta(days=k)
    return frame[~((frame["d"] == bad) & (frame["et"] == 2))].reset_index(drop=True), bad


def test_extreme_baseline_day_is_skipped(base):
    frame, bad = _corrupt_baseline(base)
    assert baseline_skip_days(frame, DAY) == [bad.strftime("%Y-%m-%d")]
    naive = detect(frame, DAY)                                   # 不跳过：基线被拉低，当天看起来在涨
    robust = detect(frame, DAY, skip_days=baseline_skip_days(frame, DAY))
    assert naive["rel"] > 0.12 and abs(robust["rel"]) < 0.1
    concl = auto_diagnose(frame, DAY)
    assert concl["pattern"] == "normal" and "基线跳过了极端日" in concl["hypotheses"][0]


def test_injection_found_despite_extreme_baseline_day(base):
    frame, _ = _corrupt_baseline(base)
    frame = drop_purchases(frame, frame["brand"] == "alpha", 0.7)
    concl = auto_diagnose(frame, DAY)
    cause = concl["root_causes"][0]
    assert (cause["dimension"], cause["value"]) == ("brand", "alpha")


def test_explanatory_set_rules():
    """空值元素不进集合；要 4 个以上元素才能凑够解释度的，不算集中。"""
    from agent.analysis.attribution import explanatory_set
    many = pd.DataFrame({"ep_local": [0.15] * 5, "lift": [2.0] * 5, "surprise": [0.01] * 5},
                        index=[f"h{i}" for i in range(5)])
    assert explanatory_set(many)[0] == []
    with_null = pd.DataFrame({"ep_local": [0.9, 0.5], "lift": [3.0, 2.0], "surprise": [0.05, 0.01]},
                             index=["(无品牌)", "beta"])
    assert explanatory_set(with_null)[0] == []          # 只剩 beta，解释 50% 不够 60%
    two = pd.DataFrame({"ep_local": [0.4, 0.3, 0.2], "lift": [2.0, 2.0, 2.0], "surprise": [0.03, 0.02, 0.01]},
                       index=["a", "b", "c"])
    assert explanatory_set(two)[0] == ["a", "b"]


def test_factor_when_element_goes_to_zero(base):
    """购买被全部删掉时 LMDI 没有定义：有浏览没购买 → CVR；用户全没了 → UV。"""
    frame = drop_purchases(base, base["brand"] == "alpha", 1.0)
    assert element_table(frame, DAY, "brand").loc["alpha", "main_factor"] == "CVR"
    frame = drop_users(base, base["user_type"] == "new", 1.0)
    assert element_table(frame, DAY, "user_type").loc["new", "main_factor"] == "UV"


def test_subset_without_purchases_is_not_data_issue(base, monkeypatch):
    """子集里没有购买（某品牌断货）是转化归零，工具不能提示「埋点丢失」。"""
    import agent.analysis.data as D
    from agent import diagnose_tools as T
    frame = drop_purchases(base, base["brand"] == "alpha", 1.0)
    monkeypatch.setattr(D, "load_window", lambda day, database=None: frame)
    sub = T.detect_anomaly(DAY.isoformat(), filters={"brand": "alpha"})
    assert "疑似埋点丢失" not in sub and "只在全站层面检查" in sub
    site = T.detect_anomaly(DAY.isoformat())
    assert "疑似埋点丢失" not in site and "数量正常" in site


def test_user_type_blocked_before_oct_15(monkeypatch):
    """10-15 之前新老用户不可靠（左截断），工具直接拒绝。d05 里 Agent 曾在 10-12 用这个维度得出错误结论。"""
    import agent.analysis.data as D
    from agent import diagnose_tools as T
    early = date(2019, 10, 12)
    frame = make_frame(day=early)
    monkeypatch.setattr(D, "load_window", lambda day, database=None: frame)
    d = early.isoformat()
    for out in [T.call_analysis_tool("drill_down", {"date": d, "dimension": "user_type"}),
                T.call_analysis_tool("detect_anomaly", {"date": d, "filters": {"user_type": "new"}}),
                T.call_analysis_tool("verify_exclusion", {"date": d, "exclude": [{"user_type": "new"}]}),
                T.call_analysis_tool("rank_dimensions", {"date": d, "dims": ["user_type", "brand"]})]:
        assert out.startswith("错误") and "左截断" in out
    assert not T.call_analysis_tool("drill_down", {"date": d, "dimension": "brand"}).startswith("错误")


# ---------------------------------------------------------------------------
# 3. 输入检查
# ---------------------------------------------------------------------------
def test_parse_day_bounds():
    assert parse_day("2019-10-08") == date(2019, 10, 8)
    with pytest.raises(ValueError):
        parse_day("2019-10-05")      # 没有 7 天基线
    with pytest.raises(ValueError):
        parse_day("2019-12-01")      # 超出数据范围
    with pytest.raises(ValueError):
        parse_day("10/24")


def test_normalize_filters():
    assert normalize_filters({"brand": "Samsung", "hour": "13", "level1": ""}) == {"brand": "samsung", "hour": 13}
    assert normalize_filters({"user_type": "新用户"}) == {"user_type": "new"}
    assert normalize_filters({"brand": "(无品牌)"}) == {"brand": "(无品牌)"}
    with pytest.raises(ValueError):
        normalize_filters({"country": "us"})
    with pytest.raises(ValueError):
        normalize_filters({"hour": 30})

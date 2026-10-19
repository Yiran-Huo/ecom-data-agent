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

import pytest

from agent.analysis import config as C
from agent.analysis.anomaly import detect
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
    assert [g["hour"] for g in det["hour_gaps"]] == [10, 11, 12]
    assert auto_diagnose(frame, DAY)["pattern"] == "data_issue"


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

"""
SOP 第 3、4 步：维度定位和剔除验证（纯函数）。

element_table：某个维度下每个元素的 ΔGMV、贡献占比、四个因子各自的变化。
    贡献占比 EP = ΔGMV_i ÷ ΔGMV。每笔购买只属于一个品类 / 品牌 / 价格段，所以 Σ ΔGMV_i = ΔGMV。

rank_dimensions：哪个维度最能「集中地」解释这次变化。思路参考微软的 Adtributor（NSDI 2014）：
    1. 解释力 EP：元素解释了多少 ΔGMV
    2. 意外度 surprise：元素在 GMV 里的占比，从基线 p 变成当天 q，变化有多意外（JS 散度的一项）
           s = ½ [ p·ln(2p/(p+q)) + q·ln(2q/(p+q)) ]
       占比没变（大家等比例下跌）→ s ≈ 0；占比变化越大 → s 越大
    3. 不成比例度 lift = 元素自身的变化率 ÷ 其余元素合起来的变化率
       （例：某品牌下降 60%，其余品牌合计只下降 27% → lift = 2.2；大家等比例下跌 → lift ≈ 1）
       其余部分反方向变化或不变时 lift = ∞
    4. 对每个维度：只保留 EP ≥ 10% 且 lift ≥ 1.5 的元素（空值元素除外），按 s 从大到小累加，
       累计 EP ≥ 60% 就停，得到这个维度的「解释集合」；超过 3 个元素还凑不够，就不算集中
    5. 得分 = 集合里 s 之和 ÷ √(集合大小)。除以 √k 是为了偏向「更简洁」的解释：
       一个元素就能解释的维度，比需要好几个元素凑出来的维度更可信（几个元素凑数时，常常混进了随机波动）
    6. 得分最高的维度最值得下钻；没有任何维度形成解释集合 → 全面性变化（broad），不要硬挑元素

verify_exclusion：把定位到的元素剔除后（当天和基线都剔除），剩下的部分还异常吗？
"""
from datetime import date

import numpy as np
import pandas as pd

from agent.analysis import config as C
from agent.analysis.anomaly import detect
from agent.analysis.decompose import lmdi
from agent.analysis.metrics import compare, daily_table, exclude, row_factors


def _surprise(p: float, q: float) -> float:
    m = (p + q) / 2
    term = lambda x: x * np.log(x / m) if x > 0 else 0.0
    return 0.5 * (term(p) + term(q)) if m > 0 else 0.0


def element_table(frame: pd.DataFrame, day: date, dim: str, baseline: str = "prev7",
                  total_delta: float | None = None, skip_days=()) -> pd.DataFrame:
    """
    每个元素一行。列：
        base_gmv, cur_gmv, delta, ep（贡献占比）, p（基线占比）, q（当天占比）,
        own_rate（自身变化率）, rest_rate（其余元素合计的变化率）, lift（不成比例度）, surprise
        UV / CVR / F / P 的相对变化（元素内部是哪个因子在变）
        main_factor：元素内部 LMDI 贡献最大的因子
    total_delta：用来算 ep 的分母，默认是这个 frame 自己的 ΔGMV；
                 下钻到子集时，传入全站的 ΔGMV，可以得到「占全站变化的比例」。
    """
    table = compare(daily_table(frame, day, dim), day, baseline, skip_days)
    base_total, cur_total = table["base_gmv"].sum(), table["cur_gmv"].sum()
    own_delta = cur_total - base_total
    denom = total_delta if total_delta else own_delta

    out = pd.DataFrame(index=table.index)
    out["base_gmv"] = table["base_gmv"]
    out["cur_gmv"] = table["cur_gmv"]
    out["delta"] = out["cur_gmv"] - out["base_gmv"]
    out["ep"] = out["delta"] / denom if denom else np.nan
    out["ep_local"] = out["delta"] / own_delta if own_delta else np.nan
    out["p"] = out["base_gmv"] / base_total if base_total else 0.0
    out["q"] = out["cur_gmv"] / cur_total if cur_total else 0.0
    sign = np.sign(own_delta) if own_delta else 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        out["own_rate"] = np.where(out["base_gmv"] > 0, out["delta"] / out["base_gmv"],
                                   np.sign(out["delta"]) * np.inf)
        rest_base = base_total - out["base_gmv"]
        out["rest_rate"] = np.where(rest_base > 0, (own_delta - out["delta"]) / rest_base, np.nan)
        own_s, rest_s = sign * out["own_rate"], sign * out["rest_rate"]
        out["lift"] = np.select(
            [rest_base <= 0, own_s <= 0, rest_s <= 0],   # 只有一个元素 / 元素反方向变化 / 其余部分没跟着变
            [1.0, 0.0, np.inf],
            default=own_s / rest_s,
        )
    out["surprise"] = [_surprise(p, q) for p, q in zip(out["p"], out["q"])]

    changes = {f: [] for f in C.FACTORS}
    main = []
    for _, row in table.iterrows():
        f0, f1 = row_factors(row, "base_"), row_factors(row, "cur_")
        for f in C.FACTORS:
            changes[f].append(f1[f] / f0[f] - 1 if f0[f] and not np.isnan(f0[f]) and not np.isnan(f1[f]) else np.nan)
        res = lmdi({k: row[f"cur_{k}"] for k in ["uv", "buyers", "n", "gmv"]},
                   {k: row[f"base_{k}"] for k in ["uv", "buyers", "n", "gmv"]})
        if res["ok"] and res["delta"]:
            main.append(max(C.FACTORS, key=lambda f: res["factors"][f]["share"]))
        elif row["base_uv"] > 0 and row["cur_uv"] == 0:
            main.append("UV")    # 浏览用户全没了（例如这部分用户整天没来）
        elif row["base_buyers"] > 0 and row["cur_buyers"] == 0:
            main.append("CVR")   # 还有人浏览，但没人买了（例如断货）：LMDI 在 0 处没有定义，直接判为转化
        else:
            main.append("")
    for f in C.FACTORS:
        out[f"{f}_change"] = changes[f]
    out["main_factor"] = main
    out.index = out.index.astype(str) if dim != "hour" else out.index
    return out.sort_values("ep_local", ascending=False)


def explanatory_set(table: pd.DataFrame) -> tuple[list, float, float]:
    """
    Adtributor 的解释集合。返回 (元素列表, 累计解释度, 得分)；不集中时元素列表为空。
    - 空值元素（(未分类)、(无品牌)）不能当成原因，不进入集合
    - 集合最多 MAX_SET_SIZE 个元素，超过就不算集中
    """
    nulls = set(C.NULL_LABELS.values())
    cands = table[(table["ep_local"] >= C.MIN_EP) & (table["lift"] >= C.MIN_LIFT)
                  & ~table.index.astype(str).isin(nulls)]
    cands = cands.sort_values("surprise", ascending=False)
    chosen, cum, score = [], 0.0, 0.0
    for element, row in cands.iterrows():
        if len(chosen) == C.MAX_SET_SIZE:
            break
        chosen.append(element)
        cum += row["ep_local"]
        score += row["surprise"]
        if cum >= C.TARGET_EEP:
            return chosen, cum, score / np.sqrt(len(chosen))
    return [], cum, 0.0


def rank_dimensions(frame: pd.DataFrame, day: date, baseline: str = "prev7",
                    dims: list[str] | None = None, skip: set | None = None, skip_days=()) -> list[dict]:
    """
    对每个候选维度算解释集合和得分，按得分从高到低返回：
        [{dim, concentrated, elements: [{value, ep, p, lift, surprise, main_factor}], explained, score, top}, ...]
    skip：已经作为过滤条件的维度（在 brand=X 里面再按 brand 拆没有意义）
    """
    dims = [d for d in (dims or C.DIMENSIONS) if d not in (skip or set())]
    if day < C.USER_TYPE_START and "user_type" in dims:
        dims.remove("user_type")
    results = []
    for dim in dims:
        table = element_table(frame, day, dim, baseline, skip_days=skip_days)
        chosen, cum, score = explanatory_set(table)
        top_value = table.index[0] if len(table) else None
        results.append({
            "dim": dim,
            "concentrated": bool(chosen),
            "elements": [_element_info(table, v) for v in chosen],
            "explained": cum if chosen else 0.0,
            "score": score,
            "top": _element_info(table, top_value) if top_value is not None else None,
        })
    return sorted(results, key=lambda r: (-r["score"], -(r["top"]["ep"] if r["top"] else 0)))


def _element_info(table: pd.DataFrame, value) -> dict:
    row = table.loc[value]
    return {
        "value": value,
        "ep": float(row["ep_local"]),
        "p": float(row["p"]),
        "lift": float(row["lift"]),
        "own_rate": float(row["own_rate"]),
        "rest_rate": float(row["rest_rate"]) if row["rest_rate"] == row["rest_rate"] else None,
        "surprise": float(row["surprise"]),
        "delta": float(row["delta"]),
        "main_factor": row["main_factor"],
    }


def verify_exclusion(frame: pd.DataFrame, day: date, conditions: list[dict], baseline: str = "prev7",
                     skip_days=()) -> dict:
    """
    剔除验证。conditions 例如 [{"brand": "samsung"}] 或 [{"level1": "electronics", "brand": "samsung"}]。
    返回：剔除前后的变化幅度和 z、剔除部分解释了多少 ΔGMV、剩下的部分是否仍然异常。
    """
    before = detect(frame, day, baseline, skip_days)
    rest = exclude(frame, conditions)
    after = detect(rest, day, baseline, skip_days)
    explained = 1 - after["change"] / before["change"] if before["change"] else np.nan
    return {
        "before": {k: before[k] for k in ["cur", "base", "change", "rel", "z", "is_anomaly"]},
        "after": {k: after[k] for k in ["cur", "base", "change", "rel", "z", "is_anomaly"]},
        "explained": explained,
        "residual_anomaly": after["is_anomaly"],
    }


def total_lmdi(frame: pd.DataFrame, day: date, baseline: str = "prev7", skip_days=()) -> dict:
    """整个 frame 的因子拆解（frame 可以是过滤后的子集）。"""
    row = compare(daily_table(frame, day), day, baseline, skip_days).iloc[0]
    return lmdi({k: row[f"cur_{k}"] for k in ["uv", "buyers", "n", "gmv"]},
                {k: row[f"base_{k}"] for k in ["uv", "buyers", "n", "gmv"]})

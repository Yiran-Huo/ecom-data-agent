"""
SOP 第 2 步：GMV 因子拆解（纯函数）。

    GMV = UV × CVR × F × P

用 LMDI（对数平均迪氏指数法）把 ΔGMV 分到四个因子上：

    ΔGMV_X = L(GMV₁, GMV₀) × ln(X₁ / X₀)，    L(a, b) = (a − b) / (ln a − ln b)

因为 ln(GMV₁/GMV₀) = Σ ln(X₁/X₀)，四项相加严格等于 ΔGMV，没有残差；
也不像连环替代法那样「先替换哪个因子，结果就不一样」。
"""
import math

import numpy as np

from agent.analysis import config as C
from agent.analysis.metrics import factors


def log_mean(a: float, b: float) -> float:
    """对数平均 L(a, b)。a == b 时极限就是 a。"""
    if a == b:
        return a
    return (a - b) / (math.log(a) - math.log(b))


def lmdi(cur: dict, base: dict) -> dict:
    """
    cur / base：{"uv", "buyers", "n", "gmv"} 四个基础量。
    返回：每个因子的 base / cur / change（相对变化）/ contribution（对 ΔGMV 的贡献）/ share（贡献 ÷ ΔGMV），
    以及 delta（ΔGMV）、ok（能不能拆：任何一个因子为 0 或缺失时 LMDI 没有定义）。
    """
    f0 = factors(base["uv"], base["buyers"], base["n"], base["gmv"])
    f1 = factors(cur["uv"], cur["buyers"], cur["n"], cur["gmv"])
    g0, g1 = base["gmv"], cur["gmv"]
    delta = g1 - g0
    ok = all(v is not None and not np.isnan(v) and v > 0 for v in list(f0.values()) + list(f1.values()))

    result = {"delta": delta, "ok": ok, "factors": {}}
    weight = log_mean(g1, g0) if ok else np.nan
    for name in C.FACTORS:
        x0, x1 = f0[name], f1[name]
        contribution = weight * math.log(x1 / x0) if ok else np.nan
        result["factors"][name] = {
            "base": x0,
            "cur": x1,
            "change": (x1 / x0 - 1) if ok else np.nan,
            "contribution": contribution,
            "share": contribution / delta if ok and delta else np.nan,
        }
    return result


def main_factors(result: dict, top: int = 2, min_share: float = 0.25) -> list[str]:
    """
    主导因子：和 ΔGMV 同方向、贡献占比 ≥ 25% 的因子，按贡献大小排序，最多取 2 个。
    （有时两个因子一起变，例如删掉一部分购买会同时降低 CVR 和 F）
    """
    if not result["ok"] or not result["delta"]:
        return []
    ranked = sorted(C.FACTORS, key=lambda f: -result["factors"][f]["share"])
    return [f for f in ranked if result["factors"][f]["share"] >= min_share][:top]

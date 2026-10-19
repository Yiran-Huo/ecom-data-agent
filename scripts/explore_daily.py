"""
Phase 0 数据探查：在定阈值、写场景之前，先看清楚真实数据「平时」长什么样。

运行方式（在项目根目录，导入完 10–11 月数据之后）：
    python scripts/explore_daily.py

回答这几个问题，结果写进 docs/phase0_report.md：
    1. 每天的 GMV、UV、CVR、F、P 是什么水平，自然波动有多大？
    2. 按现在的阈值（agent/analysis/config.py），真实数据里有几天会被判成异常？是哪几天？
       → 阈值太松（自然数据天天报警）或太严（明显的波动也不报）都要调整
    3. 成交价的分位数 → 价格段的分段点要不要改？
    4. 每天新用户占多少？→ 新用户场景的强度怎么设
    5. 大品牌的日 GMV 波动有多大？→ 品牌级别的噪声
    6. 每个小时平均多少事件？→ 数据完整性检查的最低事件数
    7. specs.json 里的排查日，在真实数据里本身是否就异常？→ 是的话要换日期

输出：
    docs/phase0_report.md                  报告（含建议）
    outputs/explore/daily_metrics.csv      每日指标
    outputs/explore/daily_metrics.png      每日指标走势图（标出按现阈值判为异常的日子）
"""
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.analysis import config as C  # noqa: E402

OUT_DIR = ROOT / "outputs" / "explore"
REPORT_PATH = ROOT / "docs" / "phase0_report.md"
SPECS_PATH = ROOT / "eval" / "scenarios" / "specs.json"

# 图的配色（dataviz 参考调色板）：单一序列用一个蓝色，标注用次要文字色
INK, INK_2, SERIES, GRID, SURFACE, FLAG = "#0b0b0b", "#52514e", "#2a78d6", "#e6e5e1", "#fcfcfb", "#e34948"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def query(sql: str, params: dict | None = None) -> pd.DataFrame:
    from agent.analysis.data import _read_sql
    return _read_sql(sql, params or {}, None)


def fetch_daily() -> pd.DataFrame:
    from agent.analysis.data import daily_series_sql
    parts = []
    start = C.DATA_START
    while start <= C.DATA_END:   # 按月查，单条查询不会太久
        end = min((pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date(), C.DATA_END)
        log(f"  每日指标 {start} ~ {end} ...")
        parts.append(daily_series_sql(start, end))
        start = end + timedelta(days=1)
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------------------
# 计算（纯函数）
# ---------------------------------------------------------------------------
def add_factors_and_flags(daily: pd.DataFrame) -> pd.DataFrame:
    """加上四个因子，以及「当天 vs 前 7 天」的变化幅度、z 分数、按现阈值是否异常。"""
    df = daily.sort_values("d").reset_index(drop=True).copy()
    df["CVR"] = df["buyers"] / df["uv"]
    df["F"] = df["n"] / df["buyers"]
    df["P"] = df["gmv"] / df["n"]
    prev = df["gmv"].shift(1).rolling(C.BASELINE_DAYS)
    df["base"] = prev.mean()
    df["std"] = prev.std()
    df["rel"] = df["gmv"] / df["base"] - 1
    df["z"] = (df["gmv"] - df["base"]) / df["std"]
    df["wow"] = df["gmv"] / df["gmv"].shift(7) - 1
    df["flag"] = (df["rel"].abs() >= C.REL_THRESHOLD) & (df["z"].abs() >= C.Z_THRESHOLD)
    return df


def suggest_threshold(df: pd.DataFrame) -> dict:
    """自然数据里 |变化幅度| 的分布，以及「大约 5% 的日子被判为异常」对应的阈值。"""
    rel = df["rel"].abs().dropna()
    z = df["z"].abs().dropna()
    q = lambda s, p: float(s.quantile(p))
    return {
        "days": len(rel),
        "rel_p50": q(rel, 0.5), "rel_p90": q(rel, 0.9), "rel_p95": q(rel, 0.95), "rel_max": float(rel.max()),
        "z_p90": q(z, 0.9), "z_p95": q(z, 0.95),
        # 取 p95 向上取整到 5%，并且不低于 10%：注入的异动要明显超过平时的波动
        "suggest_rel": max(0.10, float(np.ceil(q(rel, 0.95) * 20) / 20)),
    }


# ---------------------------------------------------------------------------
# 图：每个指标一个小图，共用时间轴（不用双坐标轴）
# ---------------------------------------------------------------------------
def plot(df: pd.DataFrame, path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti TC", "SimHei",
                                       "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    panels = [("gmv", "GMV（万）", lambda v: v / 1e4), ("uv", "UV 浏览用户数（千）", lambda v: v / 1e3),
              ("CVR", "CVR 转化率（%）", lambda v: v * 100), ("F", "F 人均购买次数", lambda v: v),
              ("P", "P 平均成交价", lambda v: v)]
    fig, axes = plt.subplots(len(panels), 1, figsize=(11, 12), sharex=True, facecolor=SURFACE)
    flagged = df[df["flag"]]
    for ax, (col, title, scale) in zip(axes, panels):
        ax.set_facecolor(SURFACE)
        ax.plot(df["d"], scale(df[col]), color=SERIES, linewidth=2)
        if col == "gmv" and len(flagged):
            ax.scatter(flagged["d"], scale(flagged[col]), s=40, color=FLAG, zorder=3, edgecolor=SURFACE, linewidth=2)
            for _, r in flagged.iterrows():
                below = r["rel"] < 0   # 下跌的标签放在点的下方，避免压住折线
                ax.annotate(f"{r['d']:%m-%d} {r['rel']:+.0%}", (r["d"], scale(r[col])), textcoords="offset points",
                            xytext=(0, -14 if below else 8), ha="center", fontsize=8, color=INK_2)
        ax.set_title(title, loc="left", fontsize=11, color=INK)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.ticklabel_format(axis="y", useOffset=False, style="plain")
        ax.tick_params(colors=INK_2, labelsize=9)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
    import matplotlib.dates as mdates
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    axes[0].text(0, 1.25, "每日指标（10–11 月，抽样数据）· 红点 = 按当前阈值判为异常的日子",
                 transform=axes[0].transAxes, fontsize=12, color=INK, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


# ---------------------------------------------------------------------------
def md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False, disable_numparse=True)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    log("1/6 每日指标 ...")
    df = add_factors_and_flags(fetch_daily())
    df.to_csv(OUT_DIR / "daily_metrics.csv", index=False, encoding="utf-8-sig")
    plot(df, OUT_DIR / "daily_metrics.png")
    thr = suggest_threshold(df[df["d"] >= pd.Timestamp(C.FIRST_DIAGNOSABLE)])

    log("2/6 成交价分位数 ...")
    prices = query("SELECT price FROM events WHERE event_type = 'purchase' AND price > 0")["price"].astype(float)
    qs = [0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]
    bands = pd.cut(prices, [b[0] for b in C.PRICE_BANDS] + [np.inf], labels=[b[2] for b in C.PRICE_BANDS], right=False)
    band_tbl = pd.DataFrame({"购买次数占比": bands.value_counts(normalize=True).sort_index(),
                             "GMV 占比": prices.groupby(bands, observed=False).sum() / prices.sum()})

    log("3/6 每日新用户占比 ...")
    new = query("""
        SELECT DATE(e.event_time) AS d,
               COUNT(DISTINCT CASE WHEN u.first_date = DATE(e.event_time) THEN e.user_id END) AS new_uv,
               COUNT(DISTINCT e.user_id) AS uv
        FROM events e JOIN user_first_seen u ON u.user_id = e.user_id
        WHERE e.event_type = 'view' GROUP BY d ORDER BY d""")
    new["share"] = new["new_uv"] / new["uv"]
    new["d"] = pd.to_datetime(new["d"])

    log("4/6 大品牌的日 GMV 波动 ...")
    brand = query("""
        SELECT DATE(e.event_time) AS d, p.brand, SUM(e.price) AS gmv
        FROM events e JOIN products p ON p.product_id = e.product_id
        WHERE e.event_type = 'purchase' AND e.price > 0 AND p.brand IS NOT NULL
        GROUP BY d, p.brand""")
    brand["gmv"] = brand["gmv"].astype(float)
    pivot = brand.pivot_table(index="d", columns="brand", values="gmv", aggfunc="sum").fillna(0)
    top = pivot.sum().sort_values(ascending=False).head(10).index
    brand_tbl = pd.DataFrame({
        "GMV 占比": pivot[top].sum() / brand["gmv"].sum(),
        "日 GMV 均值": pivot[top].mean(),
        "日波动 CV": pivot[top].std() / pivot[top].mean(),
    })

    log("5/6 每小时事件数 ...")
    hourly = query("SELECT HOUR(event_time) AS h, COUNT(*) / COUNT(DISTINCT DATE(event_time)) AS per_day "
                   "FROM events GROUP BY h ORDER BY h")
    hourly["per_day"] = hourly["per_day"].astype(float)

    log("6/6 检查场景日期 ...")
    specs = json.loads(SPECS_PATH.read_text(encoding="utf-8")) if SPECS_PATH.exists() else []
    by_day = df.set_index(df["d"].dt.date)
    spec_rows = []
    for s in specs:
        r = by_day.loc[date.fromisoformat(s["date"])]
        spec_rows.append({"场景": s["id"], "类型": s["type"], "日期": s["date"], "真实变化": f"{r['rel']:+.1%}",
                          "z": f"{r['z']:.1f}", "本身异常": "⚠ 是，换日期" if r["flag"] else "否"})

    # ---- 报告 ----
    fmt = lambda v, f: "—" if pd.isna(v) else format(v, f)
    daily_rows = [{"日期": f"{r['d']:%m-%d}", "周": "一二三四五六日"[r["d"].dayofweek], "GMV": fmt(r["gmv"], ",.0f"),
                   "UV": fmt(r["uv"], ","), "CVR": fmt(r["CVR"], ".2%"), "F": fmt(r["F"], ".3f"), "P": fmt(r["P"], ",.0f"),
                   "vs 前7天": fmt(r["rel"], "+.1%"), "z": fmt(r["z"], ".1f"), "vs 上周同日": fmt(r["wow"], "+.1%"),
                   "判为异常": "⚠" if r["flag"] else ""} for _, r in df.iterrows()]
    flagged = df[df["flag"]]
    min_hour = hourly["per_day"].min()
    lines = [
        "# Phase 0 数据探查报告（自动生成）",
        "",
        f"> 由 `scripts/explore_daily.py` 于 {time.strftime('%Y-%m-%d %H:%M')} 生成。数据为抽样样本（约全站用户的 1/10）。",
        "",
        "## 1. 自然波动和异常阈值",
        "",
        f"可排查的 {thr['days']} 天里，GMV 相对前 7 天均值的变化幅度：中位数 {thr['rel_p50']:.1%}，"
        f"90 分位 {thr['rel_p90']:.1%}，95 分位 {thr['rel_p95']:.1%}，最大 {thr['rel_max']:.1%}；"
        f"|z| 的 90 分位 {thr['z_p90']:.1f}，95 分位 {thr['z_p95']:.1f}。",
        "",
        f"按当前阈值（|变化| ≥ {C.REL_THRESHOLD:.0%} 且 |z| ≥ {C.Z_THRESHOLD}），真实数据里有 **{len(flagged)} 天**被判为异常："
        + ("、".join(f"{r['d']:%m-%d}（{r['rel']:+.1%}）" for _, r in flagged.iterrows()) or "无") + "。",
        "",
        f"**建议**：REL_THRESHOLD 设为 {thr['suggest_rel']:.0%}（自然波动 95 分位向上取整，且不低于 10%）。"
        "被判为异常的自然日子不要用作「无异常」场景；它们可以作为 README 里的真实案例。",
        "",
        "![每日指标](../outputs/explore/daily_metrics.png)",
        "",
        "## 2. 价格段",
        "",
        "成交价分位数：" + "，".join(f"P{int(q * 100)} = {prices.quantile(q):,.0f}" for q in qs) + "。",
        "",
        md(band_tbl.reset_index(names="价格段").assign(**{c: band_tbl[c].map("{:.1%}".format).values for c in band_tbl})),
        "",
        "**建议**：每个价格段的购买次数最好都不少于 5%，最高价段的 GMV 占比要足够大（≥ 20%），"
        "否则 high_price_mix 场景注入后变化不够明显。不满足就调整 config.py 的 PRICE_BANDS。",
        "",
        "## 3. 新用户",
        "",
        f"10-15 之后，每天浏览用户中新用户的占比：均值 {new[new['d'] >= '2019-10-15']['share'].mean():.1%}，"
        f"最低 {new[new['d'] >= '2019-10-15']['share'].min():.1%}，最高 {new[new['d'] >= '2019-10-15']['share'].max():.1%}。"
        "（10 月上旬的占比很高是左截断：数据从 10-01 开始，所有人第一次出现都算「新」。）",
        "",
        "## 4. 大品牌的日 GMV 波动",
        "",
        md(brand_tbl.reset_index(names="品牌").assign(**{
            "GMV 占比": brand_tbl["GMV 占比"].map("{:.1%}".format).values,
            "日 GMV 均值": brand_tbl["日 GMV 均值"].map("{:,.0f}".format).values,
            "日波动 CV": brand_tbl["日波动 CV"].map("{:.1%}".format).values})),
        "",
        "品牌场景的强度至少要让「品牌占比 × 强度」明显大于全站阈值，例如占比 20% 的品牌删掉 50% 的购买，全站约降 10%。",
        "",
        "## 5. 每小时事件数",
        "",
        f"每小时平均事件数最少 {min_hour:,.0f} 条（{int(hourly.loc[hourly['per_day'].idxmin(), 'h'])} 点 UTC），"
        f"最多 {hourly['per_day'].max():,.0f} 条。数据完整性检查的 HOUR_GAP_MIN_EVENTS 当前为 "
        f"{C.HOUR_GAP_MIN_EVENTS}，建议不超过最少小时事件数的一半（{min_hour / 2:,.0f}）。",
        "",
        "## 6. 场景日期检查",
        "",
        md(pd.DataFrame(spec_rows)) if spec_rows else "（没有找到 eval/scenarios/specs.json）",
        "",
        "## 7. 每日明细",
        "",
        md(pd.DataFrame(daily_rows)),
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    log(f"✅ 完成，用时 {time.time() - t0:.0f} 秒。报告：{REPORT_PATH}")


if __name__ == "__main__":
    main()

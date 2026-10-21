"""
归因 Agent 的分析工具：把 agent/analysis 里的计算包装成「输入参数 → 返回一段文字」，并附上给模型看的说明书。

和 tools.py 的约定一样：
- 参数有类型（日期、维度名、过滤条件），在这里检查，不合法就返回「错误：……」，不抛异常，模型看到后自己改
- 返回的文字尽量短：表格 + 一两句要点，控制在 3,000 字符以内
- 模型不写 SQL，只决定调哪个工具、传什么参数、怎么解读结果

工具清单（对应执行计划 5.2）：
    get_daily_series   一段时间的每日 GMV、UV、CVR、F、P（看趋势）
    detect_anomaly     SOP 第 1 步：确认异常 + 数据完整性检查
    decompose_gmv      SOP 第 2 步：LMDI 因子拆解
    rank_dimensions    SOP 第 3 步：哪个维度最能集中地解释变化
    drill_down         SOP 第 3 步：某个维度下每个元素的贡献和因子变化
    verify_exclusion   SOP 第 4 步：剔除验证
"""
from datetime import date as Date

import numpy as np
import pandas as pd

from agent.analysis import config as C
from agent.analysis import data as D
from agent.analysis.anomaly import baseline_skip_days, detect
from agent.analysis.attribution import element_table, rank_dimensions, total_lmdi, verify_exclusion
from agent.analysis.decompose import main_factors
from agent.analysis.metrics import apply_filters, factors

MAX_OUTPUT_CHARS = 3000


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
def money(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{v:,.0f}"


def pct(v, signed=True) -> str:
    if v is None or pd.isna(v):
        return "—"
    if np.isinf(v):
        return "新增" if v > 0 else "归零"
    return f"{v:+.1%}" if signed else f"{v:.1%}"


def ratio(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return "∞" if np.isinf(v) else f"{v:.2f}"


def zfmt(z) -> str:
    return "无法计算" if z is None or pd.isna(z) else f"{z:.2f}"


def scope_text(filters: dict) -> str:
    return "全站" if not filters else " 且 ".join(f"{k}={v}" for k, v in filters.items())


def element_text(dim: str, value) -> str:
    if dim == "user_type":
        return f"{value}（{C.USER_TYPES.get(str(value), '')}）"
    if dim == "hour":
        return f"{value} 点"
    return str(value)


def table(rows: list[dict]) -> str:
    if not rows:
        return "（无数据）"
    return pd.DataFrame(rows).to_markdown(index=False, disable_numparse=True)


def _truncate(s: str) -> str:
    return s if len(s) <= MAX_OUTPUT_CHARS else s[:MAX_OUTPUT_CHARS] + "\n…（输出过长，已截断。可以减小 top_k 或加过滤条件）"


def _prepare(date, filters, baseline="prev7"):
    """所有单日工具的公共准备：检查参数、取数、按过滤条件取子集。"""
    day = D.parse_day(date)
    if baseline not in C.BASELINES:
        raise ValueError(f"baseline 只能是 {', '.join(C.BASELINES)}")
    filters = D.normalize_filters(filters)
    if "user_type" in filters:
        _check_user_type(day)
    full = D.load_window(day)
    sub = apply_filters(full, filters)
    if sub.empty:
        raise ValueError(f"过滤条件 {scope_text(filters)} 下没有任何数据，检查取值是否写对（品牌、品类都是小写英文）")
    # 前 7 天里的极端日按全站判断，所有工具共用同一组，数字才能对得上
    skip = baseline_skip_days(full, day) if baseline == "prev7" else []
    return day, filters, full, sub, skip


def _check_user_type(day):
    if day < C.USER_TYPE_START:
        raise ValueError(f"{C.USER_TYPE_START} 之前不能用新老用户维度：数据从 {C.DATA_START} 开始，"
                         "这时大部分用户第一次出现都会被算成「新用户」（左截断），新老用户的变化不可信。请换其他维度。")


def skip_note(skip: list[str]) -> str:
    if not skip:
        return ""
    return (f"注意：前 7 天里 {'、'.join(skip)} 是极端日（GMV 低于中位数的 30% 或高于 2.5 倍），"
            "已从基线和 z 分数中剔除。")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def get_daily_series(start: str, end: str, filters: dict | None = None) -> str:
    try:
        s, e = Date.fromisoformat(str(start).strip()), Date.fromisoformat(str(end).strip())
    except ValueError:
        raise ValueError("日期格式不对，请用 YYYY-MM-DD")
    if not (C.DATA_START <= s <= e <= C.DATA_END):
        raise ValueError(f"日期要在 {C.DATA_START} 至 {C.DATA_END} 之间，且 start ≤ end")
    if (e - s).days > 31:
        raise ValueError("一次最多查 31 天，请缩小范围")
    df = D.daily_series_sql(s, e, filters)
    rows = []
    for _, r in df.iterrows():
        f = factors(r["uv"], r["buyers"], r["n"], r["gmv"])
        rows.append({"日期": r["d"].strftime("%m-%d"), "周": "一二三四五六日"[r["d"].dayofweek],
                     "GMV": money(r["gmv"]), "UV": f"{int(r['uv']):,}", "CVR": pct(f["CVR"], False),
                     "F": ratio(f["F"]), "P": money(f["P"])})
    mean = df["gmv"].mean() if len(df) else np.nan
    return _truncate(f"范围：{scope_text(D.normalize_filters(filters))}，{s} 至 {e}，日均 GMV {money(mean)}\n\n" + table(rows))


def detect_anomaly(date: str, baseline: str = "prev7", filters: dict | None = None) -> str:
    day, filters, _, sub, skip = _prepare(date, filters, baseline)
    r = detect(sub, day, baseline, skip)
    verdict = f"异常（{'下跌' if r['direction'] == 'down' else '上涨'}）" if r["is_anomaly"] else "不算异常（在正常波动范围内）"
    hist = " · ".join(f"{d} {money(v)}" for d, v in r["history"].items())
    lines = [
        f"日期 {r['date']}，范围：{scope_text(filters)}，基线：{C.BASELINES[baseline]}",
        f"当天 GMV {money(r['cur'])}；基线 {money(r['base'])}；变化 {money(r['change'])}（{pct(r['rel'])}）；"
        f"z = {zfmt(r['z'])}",
        f"判定：{verdict}。阈值：|变化| ≥ {C.REL_THRESHOLD:.0%} 且 |z| ≥ {C.Z_THRESHOLD}",
        f"前 7 天每日 GMV：{hist}",
    ]
    if filters:
        lines.append("（数据完整性只在全站层面检查：子集里没有购买说明转化归零，不代表埋点丢失。"
                     "要看数据完整性，用不带 filters 的 detect_anomaly。）")
    elif r["event_gaps"]:
        gaps = "；".join(f"{g['event']}（当天 {g['cur']:,} 条，基线日均 {g['base']:,.0f} 条）" for g in r["event_gaps"])
        lines.append(f"⚠ 数据完整性：这些事件类型当天整体接近 0，而其他事件正常，疑似埋点丢失：{gaps}。"
                     "这种情况应判为数据问题（data_issue），GMV 的变化不能当成业务变化来归因。")
    if r["hour_gaps"] and not filters:
        gaps = "；".join(f"{g['hour']} 点（当天 {g['cur']:,} 条，基线均值 {g['base']:,.0f} 条）" for g in r["hour_gaps"])
        if r["hour_gap_share"] >= C.HOUR_GAP_MIN_SHARE:
            lines.append(f"⚠ 数据完整性：以下小时的事件数（浏览+加购+购买）明显偏少，疑似数据缺失或系统故障：{gaps}。"
                         f"缺失的事件约占全天的 {r['hour_gap_share']:.1%}。先把它作为数据问题报告，不要直接当成业务原因。")
        else:
            lines.append(f"数据完整性：{gaps}，事件偏少，但只占全天约 {r['hour_gap_share']:.1%}，是轻微的数据缺口，"
                         "不足以解释 GMV 的变化，照常排查即可，可以在报告里提一句。")
    elif not filters and not r["event_gaps"]:
        lines.append("数据完整性：各小时、各类事件的数量正常。")
    if skip:
        lines.append(skip_note(skip) + "也可以用 baseline=wow（上周同一天）交叉确认。")
    return "\n".join(lines)


def decompose_gmv(date: str, baseline: str = "prev7", filters: dict | None = None) -> str:
    day, filters, _, sub, skip = _prepare(date, filters, baseline)
    res = total_lmdi(sub, day, baseline, skip)
    head = f"范围：{scope_text(filters)}，日期 {day}，基线：{C.BASELINES[baseline]}，ΔGMV = {money(res['delta'])}"
    if skip:
        head += "\n" + skip_note(skip)
    if not res["ok"]:
        return head + "\n无法拆解：某个因子为 0（例如当天没有成交），请直接看 drill_down 的结果。"
    rows = []
    for f in C.FACTORS:
        x = res["factors"][f]
        fmt = (lambda v: f"{v:,.0f}") if f == "UV" else (lambda v: f"{v:.2%}") if f == "CVR" else \
              (lambda v: f"{v:.3f}") if f == "F" else money
        rows.append({"因子": f"{f}（{C.FACTOR_LABELS[f]}）", "基线": fmt(x["base"]), "当天": fmt(x["cur"]),
                     "变化": pct(x["change"]), "对 ΔGMV 的贡献": money(x["contribution"]),
                     "贡献占比": pct(x["share"], False)})
    mains = main_factors(res)
    tip = f"主导因子：{'、'.join(mains)}" if mains else "没有单个因子占主导"
    return f"{head}\n\n{table(rows)}\n\n{tip}（贡献占比 = 该因子贡献 ÷ ΔGMV，四项之和 = 100%）"


def rank_dimensions_tool(date: str, baseline: str = "prev7", filters: dict | None = None,
                         dims: list[str] | None = None) -> str:
    day, filters, _, sub, skip = _prepare(date, filters, baseline)
    if dims:
        bad = [d for d in dims if d not in C.DIMENSIONS]
        if bad:
            raise ValueError(f"没有维度 {', '.join(bad)}，可用的维度：{', '.join(C.DIMENSIONS)}")
        if "user_type" in dims:
            _check_user_type(day)
    ranking = rank_dimensions(sub, day, baseline, dims=dims, skip=set(filters), skip_days=skip)
    rows = []
    for r in ranking:
        els = "；".join(f"{element_text(r['dim'], e['value'])} 解释 {pct(e['ep'], False)}，"
                       f"自身 {pct(e['own_rate'])} vs 其余 {pct(e['rest_rate'])}" for e in r["elements"])
        top = r["top"]
        rows.append({
            "维度": f"{r['dim']}（{C.DIM_LABELS[r['dim']]}）",
            "是否集中": "是" if r["concentrated"] else "否",
            "解释集合": els or "—",
            "累计解释": pct(r["explained"], False) if r["concentrated"] else "—",
            "得分(×1000)": f"{r['score'] * 1000:.2f}",
            "贡献最大的元素": f"{element_text(r['dim'], top['value'])}（{pct(top['ep'], False)}）" if top else "—",
        })
    best = ranking[0] if ranking else None
    if best and best["concentrated"]:
        tip = (f"建议：先看 {best['dim']} 维度，变化集中在 "
               f"{'、'.join(element_text(best['dim'], e['value']) for e in best['elements'])}。")
    else:
        tip = "没有任何维度形成集中的解释集合：变化在各维度上大致等比例分布，更像全面性变化（broad），不要硬挑一个元素当原因。"
    rule = (f"判定规则：元素解释 ≥ {C.MIN_EP:.0%} 的变化，且自身变化率是其余部分的 {C.MIN_LIFT} 倍以上，"
            f"这样的元素最多 {C.MAX_SET_SIZE} 个、累计解释 ≥ {C.TARGET_EEP:.0%} 才算集中；空值元素不能当成原因。")
    note = ("\n" + skip_note(skip)) if skip else ""
    return _truncate(f"范围：{scope_text(filters)}，日期 {day}{note}\n\n{table(rows)}\n\n{tip}\n{rule}")


def drill_down(date: str, dimension: str, baseline: str = "prev7", filters: dict | None = None,
               top_k: int = 8) -> str:
    day, filters, full, sub, skip = _prepare(date, filters, baseline)
    if dimension not in C.DIMENSIONS:
        raise ValueError(f"没有维度 {dimension}，可用的维度：{', '.join(C.DIMENSIONS)}")
    if dimension in filters:
        raise ValueError(f"已经按 {dimension} 过滤了，换一个维度下钻")
    if dimension == "user_type":
        _check_user_type(day)
    top_k = max(1, min(int(top_k), 15))
    site_delta = total_lmdi(full, day, baseline, skip)["delta"]
    t = element_table(sub, day, dimension, baseline, total_delta=site_delta, skip_days=skip)
    shown, rest = t.head(top_k), t.iloc[top_k:]
    rows = []
    for value, r in shown.iterrows():
        rows.append({
            "元素": element_text(dimension, value), "基线GMV": money(r["base_gmv"]), "当天GMV": money(r["cur_gmv"]),
            "ΔGMV": money(r["delta"]), "占本层变化": pct(r["ep_local"], False), "占全站变化": pct(r["ep"], False),
            "基线占比": pct(r["p"], False), "自身变化": pct(r["own_rate"]), "其余变化": pct(r["rest_rate"]),
            "UV": pct(r["UV_change"]), "CVR": pct(r["CVR_change"]), "F": pct(r["F_change"]), "P": pct(r["P_change"]),
            "元素内主导因子": r["main_factor"] or "—",
        })
    if len(rest):
        rows.append({"元素": f"其他 {len(rest)} 个", "基线GMV": money(rest["base_gmv"].sum()),
                     "当天GMV": money(rest["cur_gmv"].sum()), "ΔGMV": money(rest["delta"].sum()),
                     "占本层变化": pct(rest["ep_local"].sum(), False), "占全站变化": pct(rest["ep"].sum(), False),
                     "基线占比": pct(rest["p"].sum(), False)})
    layer_delta = t["delta"].sum()
    head = (f"范围：{scope_text(filters)}，按 {dimension}（{C.DIM_LABELS[dimension]}）拆，日期 {day}，"
            f"基线：{C.BASELINES[baseline]}。本层 ΔGMV = {money(layer_delta)}，全站 ΔGMV = {money(site_delta)}。"
            f"按「占本层变化」从大到小排列。" + (skip_note(skip) if skip else ""))
    notes = []
    null_label = C.NULL_LABELS.get(dimension)
    if null_label and null_label in t.index:
        notes.append(f"{null_label} 是空值，不能当成一个真正的{C.DIM_LABELS[dimension]}，结论里要单独说明。")
    if dimension == "price_band":
        notes.append("价格段按商品在基线期的参考价划分（不是当天成交价），降价不会让商品换段。")
    return _truncate("\n\n".join([head, table(rows)] + notes))


def verify_exclusion_tool(date: str, exclude: list[dict], baseline: str = "prev7",
                          filters: dict | None = None) -> str:
    day, filters, _, sub, skip = _prepare(date, filters, baseline)
    if not exclude:
        raise ValueError("exclude 不能为空，例如 [{\"brand\": \"samsung\"}]")
    conditions = [D.normalize_filters(c) for c in exclude]
    if any("user_type" in c for c in conditions):
        _check_user_type(day)
    for c in conditions:
        if apply_filters(sub, c).empty:
            raise ValueError(f"剔除条件 {scope_text(c)} 在数据里找不到，检查取值是否写对")
    r = verify_exclusion(sub, day, conditions, baseline, skip)
    b, a = r["before"], r["after"]
    names = "、".join(scope_text(c) for c in conditions)
    verdict = "仍然异常，说明还有其他原因，需要对剩余部分继续定位" if r["residual_anomaly"] else "不再异常"
    return "\n".join([
        f"范围：{scope_text(filters)}，日期 {day}，剔除：{names}（当天和基线都剔除）",
        f"剔除前：变化 {money(b['change'])}（{pct(b['rel'])}），z = {zfmt(b['z'])}，{'异常' if b['is_anomaly'] else '不异常'}",
        f"剔除后：变化 {money(a['change'])}（{pct(a['rel'])}），z = {zfmt(a['z'])}，{verdict}",
        f"被剔除部分解释了 ΔGMV 的 {pct(r['explained'], False)}。",
    ])


# ---------------------------------------------------------------------------
# 给模型看的说明书（JSON Schema）
# ---------------------------------------------------------------------------
DATE = {"type": "string", "description": "日期，YYYY-MM-DD，范围 2019-10-08 至 2019-11-30"}
BASELINE = {"type": "string", "enum": list(C.BASELINES),
            "description": "基线：prev7 = 前 7 天日均（默认）；wow = 上周同一天"}
FILTERS = {
    "type": "object",
    "description": "过滤条件，只在这个子集里分析，例如 {\"level1\": \"electronics\"}。不传就是全站",
    "properties": {
        "level1": {"type": "string", "description": "一级品类，小写英文，例如 electronics；空值写 (未分类)"},
        "level2": {"type": "string", "description": "二级品类，例如 smartphone"},
        "brand": {"type": "string", "description": "品牌，小写，例如 samsung；空值写 (无品牌)"},
        "price_band": {"type": "string", "enum": [b[2] for b in C.PRICE_BANDS]},
        "user_type": {"type": "string", "enum": list(C.USER_TYPES)},
        "hour": {"type": "integer", "description": "UTC 小时 0–23"},
    },
    "additionalProperties": False,
}
DIMENSION = {"type": "string", "enum": C.DIMENSIONS,
             "description": "level1 一级品类 / level2 二级品类 / brand 品牌 / price_band 价格段 / "
                            "user_type 新老用户 / hour 小时"}


def _fn(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


ANALYSIS_SPECS = {
    "get_daily_series": _fn(
        "get_daily_series",
        "查一段时间（最多 31 天）每天的 GMV、UV、CVR、F（人均购买次数）、P（平均成交价），用来看趋势和周内规律。",
        {"start": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
         "end": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
         "filters": {**FILTERS, "description": FILTERS["description"] + "（不支持 price_band）"}},
        ["start", "end"]),
    "detect_anomaly": _fn(
        "detect_anomaly",
        "SOP 第 1 步：判断某天的 GMV 和基线相比是否异常（变化幅度 + z 分数），并检查各小时数据是否完整。",
        {"date": DATE, "baseline": BASELINE, "filters": FILTERS}, ["date"]),
    "decompose_gmv": _fn(
        "decompose_gmv",
        "SOP 第 2 步：把 ΔGMV 用 LMDI 拆到 UV、CVR、F、P 四个因子上，四项贡献之和等于 ΔGMV。",
        {"date": DATE, "baseline": BASELINE, "filters": FILTERS}, ["date"]),
    "rank_dimensions": _fn(
        "rank_dimensions",
        "SOP 第 3 步：对比各个维度，找出变化最集中的维度和元素（解释集合）。没有维度集中 = 全面性变化。",
        {"date": DATE, "baseline": BASELINE, "filters": FILTERS,
         "dims": {"type": "array", "items": DIMENSION, "description": "只比较这些维度，不传就比较全部"}},
        ["date"]),
    "drill_down": _fn(
        "drill_down",
        "SOP 第 3 步：按一个维度拆开，看每个元素的 ΔGMV、占变化的比例、自身变化率 vs 其余部分，以及 UV/CVR/F/P 各自的变化。"
        "加 filters 可以在上一层定位到的元素内部继续下钻。",
        {"date": DATE, "dimension": DIMENSION, "baseline": BASELINE, "filters": FILTERS,
         "top_k": {"type": "integer", "description": "显示前几个元素，默认 8，最多 15"}},
        ["date", "dimension"]),
    "verify_exclusion": _fn(
        "verify_exclusion",
        "SOP 第 4 步：把定位到的元素剔除（当天和基线都剔除），看剩下的部分是否还异常，以及被剔除部分解释了多少 ΔGMV。",
        {"date": DATE, "baseline": BASELINE, "filters": FILTERS,
         "exclude": {"type": "array", "items": FILTERS,
                     "description": "要剔除的元素列表，例如 [{\"brand\": \"samsung\"}]；一个元素内的多个条件是「且」"}},
        ["date", "exclude"]),
}

ANALYSIS_TOOLS = {
    "get_daily_series": get_daily_series,
    "detect_anomaly": detect_anomaly,
    "decompose_gmv": decompose_gmv,
    "rank_dimensions": rank_dimensions_tool,
    "drill_down": drill_down,
    "verify_exclusion": verify_exclusion_tool,
}


def call_analysis_tool(name: str, args: dict) -> str:
    """调用一个分析工具。参数错误、数据错误都转成「错误：……」文字返回。"""
    try:
        return ANALYSIS_TOOLS[name](**args)
    except TypeError as e:
        return f"错误：参数不对（{e}）"
    except ValueError as e:
        return f"错误：{e}"
    except Exception as e:   # 数据库超时等
        return f"错误：工具运行失败（{type(e).__name__}: {str(e)[:300]}）"

"""
归因评测：在每个场景库上运行排查，和标准答案对比打分（执行计划第 7 节）。

先运行 scripts/build_scenarios.py 建好场景，然后（在项目根目录）：
    python eval/run_diagnosis_eval.py                              # 纯算法基线，dev 场景（不调用模型，几分钟）
    python eval/run_diagnosis_eval.py --variants v3                # 归因 Agent v3，dev 场景
    python eval/run_diagnosis_eval.py --variants v0 v1 v2 v3 --repeats 3 --tag ablation   # 消融实验
    python eval/run_diagnosis_eval.py --ids d01 d05 --variants v3  # 只跑几个场景（调试用）
    python eval/run_diagnosis_eval.py --split test --variants algo v0 v3 --repeats 3 --tag final  # 最后只跑一次

版本：
    algo  纯算法基线（不用大模型）
    v0    现在的问数 Agent（只有 SQL 工具）
    v1    + 分析工具
    v2    + SOP 和典型模式
    v3    + 计划和强制剔除验证

结果：
    eval/results/<时间>_diagnosis_<范围>_<tag>.csv   每次运行的明细（结论、各项得分、步数、token、日志路径）
    eval/results/diagnosis_summary.md               每个版本追加一行汇总
"""
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.analysis.data import load_window  # noqa: E402
from agent.analysis.pipeline import auto_diagnose  # noqa: E402
from agent.db import use_database  # noqa: E402
from agent.prompts_diagnose import VARIANTS  # noqa: E402
from eval.scoring import score, summarize  # noqa: E402

RESOLVED_PATH = ROOT / "eval" / "scenarios" / "resolved.json"
RESULTS_DIR = ROOT / "eval" / "results"
SUMMARY_PATH = RESULTS_DIR / "diagnosis_summary.md"
ALL_VARIANTS = ["algo"] + VARIANTS

SUMMARY_HEADER = (
    "| 时间 | 范围 | 版本 | tag | 模型 | 场景×次数 | Top-1 定位 | 异常判定 | 误报率 | pattern | 全站因子 | 元素因子 "
    "| 多原因召回 | 贡献误差≤15pp | 无结论 | 平均步数 | 平均秒数 | 平均费用 |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
)


def load_scenarios(split: str, ids: list[str] | None, include_invalid: bool) -> list[dict]:
    if not RESOLVED_PATH.exists():
        sys.exit("还没有场景，先运行：python scripts/build_scenarios.py")
    scenarios = json.loads(RESOLVED_PATH.read_text(encoding="utf-8"))
    if ids:
        return [s for s in scenarios if s["id"] in ids]
    if split != "all":
        scenarios = [s for s in scenarios if s["split"] == split]
    invalid = [s["id"] for s in scenarios if not s["valid"]]
    if invalid and not include_invalid:
        print(f"跳过不成立的场景：{', '.join(invalid)}（原因见 eval/scenarios/report.md）")
        scenarios = [s for s in scenarios if s["valid"]]
    return scenarios


def run_one(scn: dict, variant: str, tag: str, repeat: int) -> dict:
    """在一个场景上运行一次，返回明细行。"""
    meta = {"steps": 0, "tool_errors": 0, "rejected": 0, "nudged": False, "stopped_by_limit": False,
            "prompt_tokens": 0, "completion_tokens": 0, "cost": None, "seconds": None, "log_path": "", "error": ""}
    pred = None
    t0 = time.time()
    try:
        with use_database(scn["db"]):
            if variant == "algo":
                frame = load_window(scn["date"])
                pred = auto_diagnose(frame, date.fromisoformat(scn["date"]))
                meta["seconds"] = round(time.time() - t0, 2)
            else:
                from agent.diagnose import run_diagnosis   # 只有用到模型时才导入（需要 API key）
                r = run_diagnosis(scn["question"], variant=variant,
                                  log_tags={"eval_id": scn["id"], "tag": tag, "repeat": repeat})
                pred = r["conclusion"]
                meta.update(steps=len(r["steps"]), tool_errors=sum(s["is_error"] for s in r["steps"]),
                            rejected=r["rejected"], nudged=r["nudged"], stopped_by_limit=r["stopped_by_limit"],
                            prompt_tokens=r["usage"]["prompt_tokens"], completion_tokens=r["usage"]["completion_tokens"],
                            cost=r["cost"], seconds=r["seconds"], log_path=r["log_path"])
    except Exception as e:   # 接口超时等：这次记为没有结论，继续下一个
        meta["error"] = repr(e)[:300]
    s = score(pred, scn["expected"])
    return {"variant": variant, "repeat": repeat, "id": scn["id"], "split": scn["split"], "type": scn["type"],
            "date": scn["date"], **s, **meta,
            "expected": json.dumps(scn["expected"], ensure_ascii=False),
            "predicted": json.dumps(pred, ensure_ascii=False) if pred else ""}


def describe(pred_json: str) -> str:
    if not pred_json:
        return "（无结论）"
    p = json.loads(pred_json)
    causes = ", ".join(f"{c['dimension']}={c['value']}({c.get('factor', '')})" for c in p["root_causes"])
    return f"{p['pattern']} {causes}".strip()


def report(df: pd.DataFrame, variant: str, scope: str, tag: str, model: str):
    s = summarize(df.to_dict("records"))
    pct = lambda v: "—" if v is None else f"{v:.0%}"
    num = lambda col, fmt: "—" if df[col].dropna().empty else format(df[col].dropna().mean(), fmt)

    print("\n" + "=" * 60)
    print(f"[{variant}] {s['n']} 次运行 · Top-1 定位 {pct(s['top1'])} · 异常判定 {pct(s['anomaly_ok'])} · "
          f"误报率 {pct(s['false_alarm'])} · 全站因子 {pct(s['factor_ok'])} · 元素因子 {pct(s['cause_factor_ok'])} · "
          f"多原因召回 {pct(s['recall'])} · 无结论 {s['no_conclusion']}")
    by_type = df.groupby("type")["top1"].mean()
    print("按场景类型的 Top-1：" + " · ".join(f"{t} {v:.0%}" for t, v in by_type.items()))
    wrong = df[~df["top1"].astype(bool)]
    if len(wrong):
        print("没答对的（打开日志看过程）：")
        for _, w in wrong.iterrows():
            where = str(w["log_path"]).replace(str(ROOT) + "/", "") or w["error"]
            print(f"  {w['id']} #{w['repeat']} {w['type']}：Agent 给出 {describe(w['predicted'])}  {where}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if not SUMMARY_PATH.exists():
        SUMMARY_PATH.write_text(SUMMARY_HEADER, encoding="utf-8")
    runs = f"{df['id'].nunique()}×{df['repeat'].nunique()}"
    with SUMMARY_PATH.open("a", encoding="utf-8") as f:
        f.write(f"| {time.strftime('%Y-%m-%d %H:%M')} | {scope} | {variant} | {tag or '-'} | "
                f"{'-' if variant == 'algo' else model} | {runs} | {pct(s['top1'])} | {pct(s['anomaly_ok'])} | "
                f"{pct(s['false_alarm'])} | {pct(s['pattern_ok'])} | {pct(s['factor_ok'])} | "
                f"{pct(s['cause_factor_ok'])} | {pct(s['recall'])} | {pct(s['share_ok'])} | {s['no_conclusion']} | "
                f"{num('steps', '.1f')} | {num('seconds', '.1f')} | {num('cost', '.4f')} |\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    parser.add_argument("--ids", nargs="*", help="只跑这些场景")
    parser.add_argument("--variants", nargs="*", default=["algo"], help=f"可选 {ALL_VARIANTS}")
    parser.add_argument("--repeats", type=int, default=1, help="每个版本重复几次（大模型有随机性，建议 3）")
    parser.add_argument("--tag", default="", help="这次评测的名字")
    parser.add_argument("--include-invalid", action="store_true", help="也跑不成立的场景")
    args = parser.parse_args()

    bad = [v for v in args.variants if v not in ALL_VARIANTS]
    if bad:
        sys.exit(f"版本写错了：{bad}，可选 {ALL_VARIANTS}")
    scenarios = load_scenarios(args.split, args.ids, args.include_invalid)
    if not scenarios:
        sys.exit("没有可以跑的场景")

    model = "-"
    if any(v != "algo" for v in args.variants):
        from agent.llm import MODEL
        model = MODEL
    scope = "-".join(args.ids) if args.ids else args.split
    rows = []
    for variant in args.variants:
        repeats = 1 if variant == "algo" else args.repeats   # 纯算法是确定的，跑一次就够
        for rep in range(1, repeats + 1):
            for i, scn in enumerate(scenarios, 1):
                print(f"[{variant} 第{rep}次 {i}/{len(scenarios)}] {scn['id']} {scn['type']} {scn['date']}", flush=True)
                row = run_one(scn, variant, args.tag, rep)
                mark = "✅" if row["top1"] else "❌"
                print(f"   {mark} {describe(row['predicted'])}" + (f"  错误：{row['error']}" if row["error"] else ""),
                      flush=True)
                rows.append(row)
        report(pd.DataFrame([r for r in rows if r["variant"] == variant]), variant, scope, args.tag, model)

    df = pd.DataFrame(rows)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{stamp}_diagnosis_{scope}{'_' + args.tag if args.tag else ''}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")

    if len(args.variants) > 1:   # 消融实验：各版本按场景类型对比
        table = df.pivot_table(index="type", columns="variant", values="top1", aggfunc="mean")
        table.loc["总体"] = df.groupby("variant")["top1"].mean()
        table = table[args.variants]
        print("\n" + "=" * 60 + "\n消融实验（Top-1 定位准确率）：")
        print((table * 100).round(0).astype(int).astype(str).add("%").to_string())
    print(f"\n明细：{path.relative_to(ROOT)}\n汇总：{SUMMARY_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

"""
评测脚本：让 Agent 逐题作答，和标准答案（gold_sql 的查询结果）对比，算出准确率。

运行方式（在项目根目录）：
    python eval/run_eval.py --show-gold         # 只看标准答案，不调用模型（先检查题目本身对不对）
    python eval/run_eval.py --split dev         # 跑开发集（20 题），平时调 Prompt 用这个
    python eval/run_eval.py --split test        # 跑测试集（10 题），只在最后报告成绩时跑
    python eval/run_eval.py --ids e01 h03       # 只跑指定的题
    python eval/run_eval.py --levels trap        # 只跑某个难度（easy / medium / hard / trap）
    python eval/run_eval.py --split dev --tag v2  # 给这次评测起个名字，方便对比

对照实验（消融实验）：同一套题，用不同配置各跑一遍，看每一项改进带来多少提升
    python eval/run_eval.py --split dev --prompt minimal no_profile full
    配置 = System Prompt 的版本：minimal（最简）/ no_profile（不含数据概况）/ full（完整）

结果：
    eval/results/<时间>_<范围>_<tag>.csv   每道题的明细（标准答案、Agent 答案、对错、轮数、token、日志路径）
    eval/results/summary.md                每次评测追加一行汇总，用来对比不同版本
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # 让脚本能 import agent.xxx

from agent.core import run_agent  # noqa: E402
from agent.db import engine  # noqa: E402
from agent.llm import MODEL  # noqa: E402
from agent.prompts import PROMPT_VARIANTS  # noqa: E402

QUESTIONS_PATH = ROOT / "eval" / "questions.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"

NUMBER_REL_TOL = 0.005   # 小数（金额、均值）：相对误差 0.5% 以内算对；整数（计数）必须完全一致
RATIO_ABS_TOL = 0.002    # 比例：绝对误差 0.002（0.2 个百分点）以内算对


# ---------------------------------------------------------------------------
# 标准答案
# ---------------------------------------------------------------------------
def load_questions(split: str, ids: list[str] | None, levels: list[str] | None = None) -> list[dict]:
    qs = [json.loads(line) for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids:
        return [q for q in qs if q["id"] in ids]
    if split != "all":
        qs = [q for q in qs if q["split"] == split]
    if levels:
        qs = [q for q in qs if q["level"] in levels]
    return qs


def gold_answer(sql: str):
    """执行标准答案 SQL，取第一行第一列。"""
    with engine.connect() as conn:
        value = conn.execute(text(sql)).scalar()
    return value


# ---------------------------------------------------------------------------
# 解析 Agent 的答案 + 判分
# ---------------------------------------------------------------------------
def extract_answer(answer_text: str | None) -> str | None:
    """从回答里找最后一个「ANSWER: xxx」。找不到返回 None。"""
    if not answer_text:
        return None
    hits = re.findall(r"ANSWER\s*[:：]\s*(.+)", answer_text)
    return hits[-1].strip().strip("`*").strip() if hits else None


def parse_number(s: str) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", s)
    if not m:
        return None
    value = float(m.group().replace(",", ""))
    return value / 100 if "%" in s else value


def normalize_text(s) -> str:
    s = str(s).strip().strip("`'\"*。.，, ").lower()
    m = re.fullmatch(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", s)   # 2019/10/16、2019年10月16日 → 2019-10-16
    if m:
        s = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def is_correct(qtype: str, gold, predicted: str | None) -> bool:
    if predicted is None or gold is None:
        return False
    if qtype == "text":
        return normalize_text(predicted) == normalize_text(gold)
    value = parse_number(predicted)
    if value is None:
        return False
    if isinstance(gold, int):   # 计数类（人数、次数）必须完全一致：差 1 也可能说明口径错了
        return value == gold
    gold = float(gold)
    if qtype == "ratio":
        if value > 1:   # 模型写成了 11.53 这种百分数
            value /= 100
        return abs(value - gold) <= RATIO_ABS_TOL
    return abs(value - gold) <= NUMBER_REL_TOL * max(abs(gold), 1e-9)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
LEVELS = ["easy", "medium", "hard", "trap"]
SUMMARY_HEADER = ("| 时间 | 范围 | 配置 | tag | 模型 | 准确率 | easy | medium | hard | trap | 平均工具调用 | 平均耗时(秒) |\n"
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")


def check_config(config: str):
    if config not in PROMPT_VARIANTS:
        sys.exit(f"配置写错了：{config}。可选 {PROMPT_VARIANTS}")


def evaluate(questions: list[dict], config: str, tag: str) -> pd.DataFrame:
    """用一种配置把所有题跑一遍，返回每道题的结果。"""
    rows = []
    for i, q in enumerate(questions, 1):
        print(f"\n[{config} {i}/{len(questions)}] {q['id']} {q['question']}", flush=True)
        try:
            r = run_agent(q["question"], prompt=config, eval_mode=True,
                          log_tags={"eval_id": q["id"], "tag": tag})
            predicted = extract_answer(r["answer"])
            row = {
                "tool_calls": len(r["steps"]),
                "tool_errors": sum(s["is_error"] for s in r["steps"]),
                "prompt_tokens": r["usage"]["prompt_tokens"],
                "completion_tokens": r["usage"]["completion_tokens"],
                "cost": r["cost"],
                "seconds": r["seconds"],
                "stopped_by_limit": r["stopped_by_limit"],
                "log_path": r["log_path"],
                "error": "",
            }
        except Exception as e:   # 接口超时等：这题记为错误，继续下一题
            predicted = None
            row = {k: None for k in ["tool_calls", "tool_errors", "prompt_tokens", "completion_tokens",
                                     "cost", "seconds", "stopped_by_limit", "log_path"]}
            row["error"] = repr(e)[:200]

        correct = is_correct(q["type"], q["gold"], predicted)
        print(f"   标准答案 {q['gold']} · Agent {predicted} · {'✅' if correct else '❌'}", flush=True)
        rows.append({"config": config, "id": q["id"], "level": q["level"], "split": q["split"],
                     "type": q["type"], "question": q["question"], "gold": q["gold"],
                     "predicted": predicted, "correct": correct, **row})
    return pd.DataFrame(rows)


def report(df: pd.DataFrame, config: str, scope: str, tag: str):
    """打印一种配置的成绩，并追加一行到 summary.md。"""
    acc = df["correct"].mean()
    by_level = df.groupby("level")["correct"].agg(["mean", "count"])
    ok = df[df["error"] == ""]
    had_error = ok[ok["tool_errors"] > 0]

    print("\n" + "=" * 60)
    print(f"[{config}] 准确率：{acc:.1%}（{int(df['correct'].sum())}/{len(df)}）")
    for level in LEVELS:
        if level in by_level.index:
            m, n = by_level.loc[level]
            print(f"  {level:<6} {m:.1%}（{int(round(m * n))}/{int(n)}）")
    if len(ok):
        print(f"平均工具调用 {ok['tool_calls'].mean():.1f} 次 · 平均耗时 {ok['seconds'].mean():.1f} 秒 · "
              f"平均 token 输入 {ok['prompt_tokens'].mean():,.0f} / 输出 {ok['completion_tokens'].mean():,.0f}")
    if len(had_error):
        print(f"自我纠错成功率：{had_error['correct'].mean():.1%}（{len(had_error)} 道题中途出现工具报错）")
    if ok["cost"].notna().any():
        print(f"平均每题费用：{ok['cost'].mean():.4f}")
    if (df["error"] != "").any():
        print(f"接口异常：{int((df['error'] != '').sum())} 题（记为错误）")
    wrong = df[~df["correct"]]
    if len(wrong):
        print("答错的题（打开对应日志看过程）：")
        for _, w in wrong.iterrows():
            log = str(w.get("log_path") or "").replace(str(ROOT) + "/", "")
            print(f"  {w['id']}  标准 {w['gold']} · Agent {w['predicted']}  {log or w['error']}")

    summary = RESULTS_DIR / "summary.md"
    if summary.exists() and "| 配置 |" not in summary.read_text(encoding="utf-8"):
        summary.rename(RESULTS_DIR / "summary_v1.md")   # 旧格式的汇总另存，不覆盖
    if not summary.exists():
        summary.write_text(SUMMARY_HEADER, encoding="utf-8")
    lv = lambda name: f"{by_level.loc[name, 'mean']:.0%}" if name in by_level.index else "-"
    avg = lambda col: f"{ok[col].mean():.1f}" if len(ok) else "-"
    with summary.open("a", encoding="utf-8") as f:
        f.write(f"| {time.strftime('%Y-%m-%d %H:%M')} | {scope} | {config} | {tag or '-'} | {MODEL} | {acc:.1%} "
                f"| {lv('easy')} | {lv('medium')} | {lv('hard')} | {lv('trap')} "
                f"| {avg('tool_calls')} | {avg('seconds')} |\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    parser.add_argument("--ids", nargs="*", help="只跑指定题号，例如 --ids e01 h03")
    parser.add_argument("--levels", nargs="*", choices=LEVELS, help="只跑指定难度")
    parser.add_argument("--prompt", nargs="*", default=["full"],
                        help="一个或多个配置，例如 minimal no_profile full（默认 full）")
    parser.add_argument("--show-gold", action="store_true", help="只打印标准答案，不调用模型")
    parser.add_argument("--tag", default="", help="这次评测的名字，例如 baseline、v2")
    args = parser.parse_args()

    questions = load_questions(args.split, args.ids, args.levels)
    if not questions:
        sys.exit("没有找到题目，检查 --split / --ids / --levels")
    for config in args.prompt:
        check_config(config)   # 先检查配置写法，免得跑到一半才报错

    print(f"计算 {len(questions)} 道题的标准答案 ...")
    for q in questions:
        q["gold"] = gold_answer(q["gold_sql"])

    if args.show_gold:
        for q in questions:
            print(f"{q['id']:>4} [{q['level']:<6}] {q['gold']!s:<16} {q['question']}")
        return

    scope = "-".join(args.ids) if args.ids else args.split + ("-" + "-".join(args.levels) if args.levels else "")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    results = []
    for config in args.prompt:
        df = evaluate(questions, config, args.tag)
        report(df, config, scope, args.tag)
        results.append(df)

    all_df = pd.concat(results, ignore_index=True)
    csv_path = RESULTS_DIR / f"{stamp}_{scope}{'_' + args.tag if args.tag else ''}.csv"
    all_df.to_csv(csv_path, index=False, encoding="utf-8-sig")   # utf-8-sig：用 Excel 打开中文不乱码

    if len(args.prompt) > 1:   # 对照实验：把几种配置的成绩放在一张表里
        table = all_df.pivot_table(index="level", columns="config", values="correct", aggfunc="mean")
        table.loc["总体"] = all_df.groupby("config")["correct"].mean()
        table = table[args.prompt].reindex([lv for lv in LEVELS if lv in table.index] + ["总体"])
        print("\n" + "=" * 60 + "\n对照实验（准确率）：")
        print((table * 100).round(1).astype(str).add("%").to_string())

    print(f"\n明细：{csv_path.relative_to(ROOT)}")
    print(f"汇总：{(RESULTS_DIR / 'summary.md').relative_to(ROOT)}")


if __name__ == "__main__":
    main()

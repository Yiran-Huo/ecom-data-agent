"""
汇总 logs/runs/ 里所有运行日志：平均轮数、工具调用、出错率、token、耗时、费用。

运行方式（在项目根目录）：
    python scripts/log_summary.py            # 全部日志
    python scripts/log_summary.py --last 20  # 最近 20 次
"""
import argparse
import json
from pathlib import Path

import pandas as pd

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "runs"


def load_runs() -> pd.DataFrame:
    rows = []
    for path in sorted(LOG_DIR.glob("*.jsonl")):
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        start = next((e for e in events if e["event"] == "run_start"), {})
        end = next((e for e in events if e["event"] == "run_end"), None)
        rows.append({
            "run_id": path.stem,
            "question": start.get("question", "")[:40],
            "finished": end is not None,
            "llm_calls": end["llm_calls"] if end else None,
            "tool_calls": end["tool_calls"] if end else None,
            "tool_errors": end["tool_errors"] if end else None,
            "prompt_tokens": end["prompt_tokens"] if end else None,
            "cached_tokens": end["cached_tokens"] if end else None,
            "completion_tokens": end["completion_tokens"] if end else None,
            "seconds": end["seconds"] if end else None,
            "cost": end.get("cost") if end else None,
            "stopped_by_limit": end["stopped_by_limit"] if end else None,
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--last", type=int, help="只看最近 N 次")
    args = parser.parse_args()

    df = load_runs()
    if df.empty:
        print("logs/runs/ 里还没有日志，先用 python cli.py 问几个问题")
        return
    if args.last:
        df = df.tail(args.last)

    done = df[df["finished"]]
    print(f"运行次数：{len(df)}（正常结束 {len(done)}，中途出错 {len(df) - len(done)}）")
    if done.empty:
        return
    cached_share = done["cached_tokens"].sum() / max(done["prompt_tokens"].sum(), 1)
    print(f"平均模型调用：{done['llm_calls'].mean():.1f} 次 · 平均工具调用：{done['tool_calls'].mean():.1f} 次")
    print(f"工具出错次数占比：{done['tool_errors'].sum() / max(done['tool_calls'].sum(), 1):.1%}"
          f" · 出现过工具报错的运行：{(done['tool_errors'] > 0).mean():.1%}")
    print(f"平均 token：输入 {done['prompt_tokens'].mean():,.0f}（缓存命中 {cached_share:.0%}）"
          f" / 输出 {done['completion_tokens'].mean():,.0f}")
    print(f"平均耗时：{done['seconds'].mean():.1f} 秒 · 最慢：{done['seconds'].max():.1f} 秒")
    print(f"触发步数上限：{int(done['stopped_by_limit'].sum())} 次")
    if done["cost"].notna().any():
        print(f"平均每题费用：{done['cost'].mean():.4f} · 合计：{done['cost'].sum():.4f}")
    else:
        print("（没有费用数据：在 .env 里填 LLM_PRICE_INPUT / LLM_PRICE_OUTPUT 后，新的运行会记录费用）")

    print("\n最近 10 次：")
    cols = ["run_id", "question", "tool_calls", "tool_errors", "seconds"]
    print(df[cols].tail(10).to_string(index=False))


if __name__ == "__main__":
    main()

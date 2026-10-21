"""
批量运行测试题，把每道题的完整过程保存成一份报告，方便回头检查。

运行方式（在项目根目录）：
    python scripts/run_questions.py          # 默认跑 Q3、Q4、Q5
    python scripts/run_questions.py 1 2 3 4 5  # 指定题号

报告保存在 logs/test_run_日期时间.md
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # 让脚本能 import agent.xxx

from agent.core import run_agent  # noqa: E402

QUESTIONS = {
    1: "10 月一共有多少次购买？涉及多少个购买用户？",
    2: "GMV 最高的 10 个品牌是哪些？",
    3: "10 月每天的 GMV 趋势如何？画一张折线图。",
    4: "用户级的「浏览 → 加购 → 购买」转化率各是多少？",
    5: "哪个一级品类的「浏览到购买」用户转化率最高？",
}


def main():
    ids = [int(x) for x in sys.argv[1:]] or [3, 4, 5]
    lines = [f"# 测试运行 {time.strftime('%Y-%m-%d %H:%M')}", ""]

    for qid in ids:
        question = QUESTIONS[qid]
        print(f"\n▶ Q{qid}：{question}", flush=True)
        try:
            r = run_agent(question, on_step=lambda s: print(f"   第 {s['round']} 轮 {s['tool']}"
                                                            f"{' ❌' if s['is_error'] else ''}", flush=True))
        except Exception as e:   # 某道题出错不影响其他题
            print(f"   出错：{e}")
            lines += [f"## Q{qid}：{question}", "", f"**运行出错**：`{e}`", ""]
            continue

        u = r["usage"]
        print(f"   完成：{len(r['steps'])} 次工具调用，{r['seconds']} 秒")
        lines += [
            f"## Q{qid}：{question}",
            "",
            f"{len(r['steps'])} 次工具调用 · {r['seconds']} 秒 · "
            f"token 输入 {u['prompt_tokens']:,} / 输出 {u['completion_tokens']:,}"
            + (f" · 图片：{', '.join(r['charts'])}" if r["charts"] else ""),
            "",
            "### 过程",
            "",
        ]
        for s in r["steps"]:
            lines += [
                f"**第 {s['round']} 轮 · {s['tool']}**{' ❌' if s['is_error'] else ''}",
                "",
                "```",
                s["arguments"],
                "```",
                "",
                "```",
                s["result"][:1500],
                "```",
                "",
            ]
        lines += ["### 回答", "", r["answer"] or "（空）", "", "---", ""]

    out = ROOT / "logs" / f"test_run_{time.strftime('%Y%m%d_%H%M%S')}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ 报告已保存：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

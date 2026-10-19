"""
命令行版 Agent。

    python cli.py                            # 对话模式：可以连续追问，Agent 记得前面聊过什么
    python cli.py "10 月一共有多少次购买？"     # 只问一个问题

    python cli.py --diagnose 2019-11-12                  # GMV 异动排查（默认 v3 版本）
    python cli.py --diagnose 2019-11-12 --variant v1     # 用其他版本排查（v0 / v1 / v2 / v3）
    python cli.py --diagnose "11 月 12 日 GMV 怎么跌了？"   # 也可以直接写问题

对话模式里的命令：
    /new      开始新对话（清空短期记忆）
    /memory   查看长期记忆
    /forget   清空长期记忆
    q         退出
"""
import json
import re
import sys

from agent.core import run_agent
from agent.memory import Conversation, clear_notes, load_notes


def show_step(record):
    """每执行完一个工具，打印一行进度。"""
    mark = "❌" if record["is_error"] else "→"
    print(f"\n[第 {record['round']} 轮] {record['tool']}")
    print(f"  参数：{record['arguments'][:300]}")
    preview = record["result"][:300].replace("\n", "\n  ")
    print(f"  {mark} {preview}{'…' if len(record['result']) > 300 else ''}")


def ask(question, conversation=None):
    result = run_agent(question, conversation=conversation, on_step=show_step)
    print("\n" + "=" * 60)
    print(result["answer"])
    if result["charts"]:
        print("\n图片：" + ", ".join(result["charts"]))
    u = result["usage"]
    cost = f"，估算费用 {result['cost']}" if result["cost"] is not None else ""
    print(f"\n（{len(result['steps'])} 次工具调用，用时 {result['seconds']} 秒，"
          f"token：输入 {u['prompt_tokens']:,}（缓存命中 {u['cached_tokens']:,}）/ 输出 {u['completion_tokens']:,}{cost}）")
    print(f"（日志：{result['log_path']}）")


def chat():
    conversation = Conversation()
    print("电商行为分析 Agent。可以连续追问；/new 新对话，/memory 看长期记忆，/forget 清空长期记忆，q 退出。")
    while True:
        q = input("\n你的问题 > ").strip()
        if q.lower() in ("q", "quit", "exit"):
            break
        if q == "/new":
            conversation.reset()
            print("已开始新对话。")
        elif q == "/memory":
            notes = load_notes()
            print("\n".join(f"- {n['note']}（{n['saved_at']}）" for n in notes) or "（还没有长期记忆）")
        elif q == "/forget":
            clear_notes()
            print("已清空长期记忆。")
        elif q:
            ask(q, conversation)


# ---------------------------------------------------------------------------
# GMV 异动排查
# ---------------------------------------------------------------------------
def show_event(kind, payload):
    if kind == "tool":
        if payload["tool"] in ("update_plan", "submit_conclusion") and not payload["is_error"]:
            return   # 计划和结论单独显示
        show_step(payload)
    elif kind == "plan":
        from agent.diagnose import render_plan
        print("\n📋 排查计划\n" + render_plan(payload))
    elif kind == "conclusion":
        print("\n✅ 已提交结构化结论")


def diagnose(target: str, variant: str = "v3"):
    from agent.diagnose import run_diagnosis

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", target.strip()):
        question = f"请排查 {target.strip()} 的 GMV 是否有异常；如果有，定位原因。"
    else:
        question = target
    print(f"问题：{question}（版本 {variant}）")
    result = run_diagnosis(question, variant=variant, on_event=show_event)
    print("\n" + "=" * 60)
    print(result["answer"] or "（没有文字报告）")
    print("\n结构化结论：")
    print(json.dumps(result["conclusion"], ensure_ascii=False, indent=2) if result["conclusion"] else "（没有提交）")
    u = result["usage"]
    cost = f"，估算费用 {result['cost']}" if result["cost"] is not None else ""
    print(f"\n（{len(result['steps'])} 次工具调用，用时 {result['seconds']} 秒，"
          f"token：输入 {u['prompt_tokens']:,}（缓存命中 {u['cached_tokens']:,}）/ 输出 {u['completion_tokens']:,}{cost}）")
    print(f"（日志：{result['log_path']}）")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--diagnose":
        if len(args) < 2:
            sys.exit("用法：python cli.py --diagnose 2019-11-12 [--variant v3]")
        variant = args[args.index("--variant") + 1] if "--variant" in args else "v3"
        diagnose(args[1], variant)
    elif args:
        ask(" ".join(args))
    else:
        chat()

"""
命令行版 Agent。

    python cli.py                            # 对话模式：可以连续追问，Agent 记得前面聊过什么
    python cli.py "10 月一共有多少次购买？"     # 只问一个问题

对话模式里的命令：
    /new      开始新对话（清空短期记忆）
    /memory   查看长期记忆
    /forget   清空长期记忆
    q         退出
"""
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


if __name__ == "__main__":
    if len(sys.argv) > 1:
        ask(" ".join(sys.argv[1:]))
    else:
        chat()

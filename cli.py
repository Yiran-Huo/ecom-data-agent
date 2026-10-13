"""
命令行版 Agent。

    python cli.py                       # 进入对话模式，可以连续提问，输入 q 退出
    python cli.py "10 月一共有多少次购买？"   # 只问一个问题
"""
import sys

from agent.core import run_agent


def show_step(record):
    """每执行完一个工具，打印一行进度。"""
    mark = "❌" if record["is_error"] else "→"
    print(f"\n[第 {record['round']} 轮] {record['tool']}")
    print(f"  参数：{record['arguments'][:300]}")
    preview = record["result"][:300].replace("\n", "\n  ")
    print(f"  {mark} {preview}{'…' if len(record['result']) > 300 else ''}")


def ask(question):
    result = run_agent(question, on_step=show_step)
    print("\n" + "=" * 60)
    print(result["answer"])
    if result["charts"]:
        print("\n图片：" + ", ".join(result["charts"]))
    u = result["usage"]
    print(f"\n（{len(result['steps'])} 次工具调用，用时 {result['seconds']} 秒，"
          f"token：输入 {u['prompt_tokens']:,} / 输出 {u['completion_tokens']:,}）")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        ask(" ".join(sys.argv[1:]))
    else:
        print("电商行为分析 Agent。输入问题后回车，输入 q 退出。")
        while True:
            q = input("\n你的问题 > ").strip()
            if q.lower() in ("q", "quit", "exit"):
                break
            if q:
                ask(q)

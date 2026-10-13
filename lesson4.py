"""
第 4 步：第一个真正的 Agent —— 大模型 + 一个 run_sql 工具 + 循环。

运行方式（在项目根目录）：
    python lesson4.py                                  # 默认问题
    python lesson4.py "购买次数最多的品牌，平均成交价是多少？"

和 lesson1.py 的结构完全一样，只是把 fake_llm 换成了真正的 DeepSeek。
"""
import json
import sys

import pandas as pd
from sqlalchemy import text

from agent.db import engine
from agent.llm import MODEL, client

MAX_ROWS = 50          # 返回给模型的最多行数，防止结果太长撑爆上下文
TIMEOUT_MS = 15_000    # 单条查询最长 15 秒
MAX_STEPS = 8          # 最多来回 8 次


# ---------------------------------------------------------------------------
# 工具：执行 SQL
# ---------------------------------------------------------------------------
def run_sql(query: str) -> str:
    """执行一条只读 SQL，返回表格文本。任何错误都返回文字，而不是让程序崩溃。"""
    q = query.strip().rstrip(";").strip()
    first_word = q.split(None, 1)[0].lower() if q else ""
    # 第一层保险：只允许查询类语句（第二层保险是数据库只读账号）
    if first_word not in ("select", "with", "show", "describe", "desc"):
        return "错误：只允许只读查询（SELECT / WITH / SHOW / DESCRIBE）"
    if ";" in q:
        return "错误：一次只能执行一条语句"

    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {TIMEOUT_MS}"))
            result = conn.execute(text(q))
            rows = result.fetchmany(MAX_ROWS + 1)   # 多取一行，用来判断是否被截断
            df = pd.DataFrame(rows, columns=list(result.keys()))
    except Exception as e:
        # 把数据库的报错原样告诉模型，它通常能自己改正
        return f"SQL 错误：{getattr(e, 'orig', e)}"

    truncated = len(df) > MAX_ROWS
    df = df.head(MAX_ROWS)
    out = df.to_markdown(index=False) if len(df) else "（查询结果为空）"
    if truncated:
        out += f"\n（结果超过 {MAX_ROWS} 行，只显示前 {MAX_ROWS} 行；需要全部数据请用聚合或 LIMIT）"
    return out


TOOLS = {"run_sql": run_sql}

RUN_SQL_SPEC = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": (
            "在电商行为数据库（MySQL 8，库名 ecom）上执行一条只读 SQL，返回结果表（最多 50 行）。"
            "可以用 SHOW TABLES、DESCRIBE 表名 查看表结构。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "一条 MySQL 8 语法的 SELECT / WITH / SHOW / DESCRIBE 语句"}
            },
            "required": ["query"],
        },
    },
}

# 第 6 步会把数据说明书放进来，这里先只给最基本的提示
SYSTEM_PROMPT = (
    "你是电商数据分析师，可以通过 run_sql 工具查询 MySQL 数据库。"
    "不清楚有哪些表、字段时，先用 SHOW TABLES 和 DESCRIBE 查看，不要猜。"
    "回答里的每个数字都必须来自查询结果。用中文回答。"
)


# ---------------------------------------------------------------------------
# Agent 主循环
# ---------------------------------------------------------------------------
def run_agent(question: str, max_steps: int = MAX_STEPS) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for step in range(1, max_steps + 1):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=[RUN_SQL_SPEC])
        msg = resp.choices[0].message

        # 情况一：没有工具调用 → 这就是最终回答
        if not msg.tool_calls:
            return msg.content

        # 情况二：要调用工具。顺序很重要：先追加模型这条回复，再追加每个工具的结果
        messages.append(msg.model_dump(exclude_none=True))

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)   # arguments 是 JSON 字符串，要先转成字典
            except json.JSONDecodeError:
                args = None

            if name not in TOOLS:
                result = f"错误：没有名为 {name} 的工具，可用的工具有：{', '.join(TOOLS)}"
            elif args is None:
                result = "错误：工具参数不是合法的 JSON"
            else:
                result = TOOLS[name](**args)

            print(f"\n—— 第 {step} 步：{name} ——")
            print(args.get("query", args) if isinstance(args, dict) else tc.function.arguments)
            print(f"→ {result[:200]}{'…' if len(result) > 200 else ''}")

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return f"超过最大步数（{max_steps} 步），没能给出回答"


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "events 表一共有多少行？"
    print(f"问题：{question}")
    print("\n====== 回答 ======\n" + run_agent(question))

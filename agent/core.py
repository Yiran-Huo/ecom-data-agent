"""
Agent 主循环。和 lesson4.py 的结构完全一样，多了四样东西：
    1. System Prompt 里带上了数据说明书（见 prompts.py）
    2. 三个正式工具（见 tools.py）
    3. 步数用完时，让模型根据已有信息给出一个「不完整但诚实」的回答
    4. 返回结构化结果：回答、每一步的记录、生成的图片、token 用量
       —— 第 7 步的日志、第 8 步的评测、第 9 步的网页都会用到

用法：
    from agent.core import run_agent
    result = run_agent("10 月 GMV 最高的一级品类是哪个？")
    print(result["answer"])
"""
import time

from agent.llm import MODEL, client
from agent.prompts import SYSTEM_PROMPT
from agent.tools import TOOL_SPECS, call_tool, find_charts

MAX_STEPS = 12


def run_agent(question: str, max_steps: int = MAX_STEPS, on_step=None) -> dict:
    """
    输入：一个问题。
    返回：{"answer": 最终回答, "steps": 每一步的记录, "charts": 图片路径, "usage": token 用量, "seconds": 用时}
    on_step：可选的回调函数，每执行完一个工具就调用一次 on_step(记录)，网页界面用它实时显示进度。
    """
    start = time.time()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    steps, charts = [], []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def ask_model(**kwargs):
        resp = client.chat.completions.create(model=MODEL, messages=messages, **kwargs)
        if resp.usage:   # 累计 token 用量，第 7 步算成本时用
            usage["prompt_tokens"] += resp.usage.prompt_tokens
            usage["completion_tokens"] += resp.usage.completion_tokens
        return resp.choices[0].message

    def finish(answer):
        return {"answer": answer, "steps": steps, "charts": charts,
                "usage": usage, "seconds": round(time.time() - start, 1)}

    for round_no in range(1, max_steps + 1):
        msg = ask_model(tools=TOOL_SPECS)

        # 没有工具调用 → 这就是最终回答
        if not msg.tool_calls:
            return finish(msg.content)

        # 先把模型「要调用工具」的这条回复放进对话，再逐个执行工具、放入结果
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            result = call_tool(tc.function.name, tc.function.arguments)
            charts += find_charts(result)

            record = {
                "round": round_no,
                "tool": tc.function.name,
                "arguments": tc.function.arguments,
                "result": result,
                "is_error": result.startswith(("错误", "SQL 错误")) or "运行出错" in result,
            }
            steps.append(record)
            if on_step:
                on_step(record)

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # 步数用完：不再给工具，让模型根据已经查到的信息作答，并说明哪里没完成
    messages.append({
        "role": "user",
        "content": "已经达到最大步数，不能再调用工具。请根据目前已经查到的结果回答，并明确说明哪些部分没有完成。",
    })
    try:
        return finish(ask_model().content)
    except Exception:
        return finish(f"超过最大步数（{max_steps} 轮），没能给出完整回答。")

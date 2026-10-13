"""
Agent 主循环。核心结构和 lesson4.py 完全一样，在此基础上加了：
    1. System Prompt 带上数据说明书（prompts.py），可切换版本做对照实验
    2. 正式工具（tools.py）
    3. 记忆：多轮对话的短期记忆 + 用户偏好的长期记忆（memory.py）
    4. 日志（tracer.py）、评测模式（ANSWER: 行）、步数用完时的收尾

消息的排列顺序（这个顺序是为了让开头部分每次都一样，命中缓存、省钱）：
    [system]     System Prompt（+ 长期记忆）              ← 每次都一样，可以命中缓存
    [历史]       之前几轮的问答 / 更早对话的摘要            ← 多轮对话时才有
    [user]       这次的问题                                ← 每次不同，放在最后
    [assistant / tool ...]  本轮的工具调用过程

用法：
    from agent.core import run_agent
    result = run_agent("10 月 GMV 最高的一级品类是哪个？")
    print(result["answer"])
"""
import time

from agent.llm import MODEL, client
from agent.memory import Conversation, render_notes
from agent.prompts import EVAL_SUFFIX, MEMORY_RULE, build_system_prompt
from agent.tools import MEMORY_SPEC, TOOL_SPECS, call_tool, find_charts
from agent.tracer import Tracer

MAX_STEPS = 12
API_TIMEOUT_S = 120   # 单次调用模型最多等 120 秒
API_RETRIES = 3       # 超时或网络错误时自动重试 3 次（openai 库内置，重试间隔会逐渐加长）


def run_agent(question: str, *, conversation: Conversation | None = None,
              prompt: str = "full",
              eval_mode: bool = False, max_steps: int = MAX_STEPS,
              on_step=None, log_tags: dict | None = None) -> dict:
    """
    输入：一个问题。
    conversation：多轮对话对象。传入时会带上之前的问答，回答完自动把这一轮记进去。
    prompt：System Prompt 版本，minimal / no_profile / full。
    eval_mode：评测时为 True：要求最后一行写 ANSWER；不加载长期记忆、不提供 save_memory 工具。
    on_step：每执行完一个工具调用一次 on_step(记录)，网页界面用它实时显示进度。
    返回：{"answer", "steps", "charts", "usage", "cost", "seconds", "run_id", "log_path", "stopped_by_limit"}
    """
    # ---- 1. 准备：System Prompt、工具 ----
    system = build_system_prompt(prompt)
    tools = list(TOOL_SPECS)
    if eval_mode:
        system += EVAL_SUFFIX
    else:
        system += MEMORY_RULE + render_notes()   # 长期记忆
        tools.append(MEMORY_SPEC)

    tracer = Tracer(question, MODEL, tags={"prompt": prompt, **(log_tags or {})})

    messages = [{"role": "system", "content": system}]
    if conversation:
        messages += conversation.messages()               # 短期记忆：之前的问答
    messages.append({"role": "user", "content": question})
    steps, charts = [], []

    def ask_model(round_no, **kwargs):
        t0 = time.time()
        resp = client.with_options(timeout=API_TIMEOUT_S, max_retries=API_RETRIES).chat.completions.create(
            model=MODEL, messages=messages, **kwargs)
        tracer.llm_call(round_no, time.time() - t0, resp.usage)
        return resp.choices[0].message

    def finish(answer, stopped_by_limit=False):
        seconds = tracer.finish(answer, stopped_by_limit)
        if conversation is not None:
            conversation.add(question, answer)            # 这一轮记进短期记忆
        t = tracer.totals
        return {"answer": answer, "steps": steps, "charts": charts, "seconds": seconds,
                "usage": {"prompt_tokens": t["prompt_tokens"], "cached_tokens": t["cached_tokens"],
                          "completion_tokens": t["completion_tokens"]},
                "cost": tracer.cost(), "run_id": tracer.run_id,
                "log_path": str(tracer.path), "stopped_by_limit": stopped_by_limit}

    # ---- 2. 主循环：调模型 → 执行工具 → 结果放回对话 ----
    try:
        for round_no in range(1, max_steps + 1):
            msg = ask_model(round_no, tools=tools)

            # 没有工具调用 → 这就是最终回答
            if not msg.tool_calls:
                return finish(msg.content)

            # 先把模型「要调用工具」的这条回复放进对话，再逐个执行工具、放入结果
            messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                t0 = time.time()
                result = call_tool(tc.function.name, tc.function.arguments)
                is_error = result.startswith(("错误", "SQL 错误")) or "运行出错" in result
                tracer.tool_call(round_no, tc.function.name, tc.function.arguments,
                                 result, is_error, time.time() - t0)
                charts += find_charts(result)

                record = {"round": round_no, "tool": tc.function.name,
                          "arguments": tc.function.arguments, "result": result, "is_error": is_error}
                steps.append(record)
                if on_step:
                    on_step(record)

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        # ---- 3. 步数用完：不再给工具，让模型根据已经查到的信息作答，并说明哪里没完成 ----
        messages.append({
            "role": "user",
            "content": "已经达到最大步数，不能再调用工具。请根据目前已经查到的结果回答，并明确说明哪些部分没有完成。",
        })
        try:
            return finish(ask_model(max_steps + 1).content, stopped_by_limit=True)
        except Exception:
            return finish(f"超过最大步数（{max_steps} 轮），没能给出完整回答。", stopped_by_limit=True)

    except Exception as e:
        # 接口超时、网络断开等意外：记进日志再抛出，方便事后排查
        tracer.log("run_error", error=repr(e))
        raise

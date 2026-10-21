"""
GMV 异动归因 Agent 的主循环。

结构和 core.py（问数 Agent）一样：调模型 → 执行工具 → 结果放回对话 → 再调模型，直到模型不再调用工具。
多出来的部分：
    1. 工具是 diagnose_tools.py 里的分析工具（v0 版本除外，v0 只有 SQL）
    2. 两个「状态工具」：update_plan（排查计划）、submit_conclusion（结构化结论）
    3. v3 版本的护栏：没写计划、或者没做剔除验证就交 concentrated 结论，会被拒绝，模型必须补上
    4. 模型忘了交结论就想结束时，提醒一次

用法：
    from agent.diagnose import run_diagnosis
    r = run_diagnosis("2019-10-24 的 GMV 有没有异常？如果有，帮我找原因", variant="v3")
    print(r["answer"]); print(r["conclusion"])
"""
import json
import os
import time

from agent.conclusion import SCHEMA, validate
from agent.diagnose_tools import ANALYSIS_SPECS, ANALYSIS_TOOLS, call_analysis_tool
from agent.prompts_diagnose import VARIANTS, build
from agent.tracer import Tracer

MAX_STEPS = 24   # 真实运行中 v3 一般用 8–19 步
API_TIMEOUT_S = 120
API_RETRIES = 5
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))   # 评测要可复现，默认 0

LEGACY_TOOLS = ["describe_table", "run_sql", "run_python"]   # 来自 tools.py

PLAN_SPEC = {"type": "function", "function": {
    "name": "update_plan",
    "description": "写下或更新本次的排查计划。第一次调用写出全部步骤；之后每完成一步就更新状态，发现新线索可以加步骤。",
    "parameters": {"type": "object", "properties": {
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "step": {"type": "string", "description": "这一步做什么，一句话"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped"]},
            "finding": {"type": "string", "description": "这一步的发现（完成后填写，一句话，含关键数字）"},
        }, "required": ["step", "status"]}},
    }, "required": ["steps"]},
}}

SUBMIT_SPEC = {"type": "function", "function": {
    "name": "submit_conclusion",
    "description": "提交结构化结论（排查的最后一步，只调用一次）。提交成功后再输出文字报告。",
    "parameters": SCHEMA,
}}


def tools_for(variant: str) -> list[dict]:
    if variant not in VARIANTS:
        raise ValueError(f"未知的版本 {variant}，可选 {VARIANTS}")
    if variant == "v0":
        from agent.tools import TOOL_SPECS   # 放在函数里导入：只有 v0 才需要
        specs = list(TOOL_SPECS)
    else:
        from agent.tools import TOOL_SPECS
        specs = list(ANALYSIS_SPECS.values()) + [s for s in TOOL_SPECS if s["function"]["name"] == "run_sql"]
    if variant == "v3":
        specs.append(PLAN_SPEC)
    return specs + [SUBMIT_SPEC]


class State:
    """一次排查过程中的状态：计划、是否做过剔除验证、结论。"""

    def __init__(self, variant: str):
        self.variant = variant
        self.plan: list[dict] = []
        self.verified = False
        self.conclusion: dict | None = None
        self.rejected = 0   # 结论被拒绝的次数（格式错误或护栏）


def render_plan(steps: list[dict]) -> str:
    mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "skipped": "[-]"}
    lines = []
    for i, s in enumerate(steps, 1):
        line = f"{mark.get(s.get('status'), '[ ]')} {i}. {s.get('step', '')}"
        if s.get("finding"):
            line += f" —— {s['finding']}"
        lines.append(line)
    return "\n".join(lines)


def execute(name: str, arguments: str, state: State) -> str:
    """执行一个工具调用，返回给模型的文字。任何问题都返回文字，不抛异常。"""
    allowed = {s["function"]["name"] for s in tools_for(state.variant)}
    if name not in allowed:
        return f"错误：没有名为 {name} 的工具，可用的工具有：{', '.join(sorted(allowed))}"
    if name in LEGACY_TOOLS:
        from agent.tools import call_tool
        return call_tool(name, arguments)
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return "错误：工具参数不是合法的 JSON"
    if not isinstance(args, dict):
        return "错误：工具参数必须是 JSON 对象"

    if name in ANALYSIS_TOOLS:
        result = call_analysis_tool(name, args)
        if name == "verify_exclusion" and not result.startswith("错误"):
            state.verified = True
        return result

    if name == "update_plan":
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) and s.get("step") for s in steps):
            return "错误：steps 必须是非空列表，每一项至少有 step 字段"
        state.plan = steps
        return "计划已更新：\n" + render_plan(steps)

    if name == "submit_conclusion":
        conclusion, errors = validate(args)
        if errors:
            state.rejected += 1
            return "错误：结论没有被接受，请修改后重新提交：\n- " + "\n- ".join(errors)
        if state.variant == "v3":
            if not state.plan:
                state.rejected += 1
                return "错误：还没有写排查计划。请先调用 update_plan。"
            if conclusion["pattern"] == "concentrated" and not state.verified:
                state.rejected += 1
                return ("错误：pattern 是 concentrated，但还没有做剔除验证。"
                        "请先用 verify_exclusion 剔除定位到的元素，确认剩下的部分回到正常后再提交。")
        state.conclusion = conclusion
        return "已收到结论。现在输出给业务同事看的文字报告（按报告格式），不要再调用工具。"

    return f"错误：工具 {name} 没有实现"


def run_diagnosis(question: str, variant: str = "v3", *, max_steps: int = MAX_STEPS,
                  on_event=None, log_tags: dict | None = None, client=None, model: str | None = None) -> dict:
    """
    输入：一个排查问题（通常包含日期）。
    on_event(kind, payload)：kind 为 "tool"（每次工具调用）、"plan"（计划更新）、"conclusion"（结论被接受）。
    client / model：默认用 agent/llm.py 里的；测试时可以传入假的 client。
    返回：{"answer", "conclusion", "plan", "steps", "usage", "cost", "seconds", "run_id", "log_path",
           "stopped_by_limit", "nudged", "rejected"}
    """
    if client is None:
        from agent.llm import MODEL, client as default_client   # 放在函数里导入：测试时不需要 API key
        client, model = default_client, model or MODEL
    model = model or "unknown"

    tools = tools_for(variant)
    state = State(variant)
    tracer = Tracer(question, model, tags={"mode": "diagnose", "variant": variant, **(log_tags or {})})
    messages = [{"role": "system", "content": build(variant)}, {"role": "user", "content": question}]
    steps, nudged, stopped = [], False, False

    def notify(kind, payload):
        if on_event:
            on_event(kind, payload)

    def ask(round_no, tool_list):
        t0 = time.time()
        resp = client.with_options(timeout=API_TIMEOUT_S, max_retries=API_RETRIES).chat.completions.create(
            model=model, messages=messages, tools=tool_list, temperature=TEMPERATURE)
        tracer.llm_call(round_no, time.time() - t0, resp.usage)
        return resp.choices[0].message

    def run_tool_calls(round_no, msg):
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            t0 = time.time()
            had_conclusion, plan_before = state.conclusion is not None, list(state.plan)
            result = execute(tc.function.name, tc.function.arguments, state)
            is_error = result.startswith(("错误", "SQL 错误")) or "运行出错" in result
            tracer.tool_call(round_no, tc.function.name, tc.function.arguments, result, is_error, time.time() - t0)
            record = {"round": round_no, "tool": tc.function.name, "arguments": tc.function.arguments,
                      "result": result, "is_error": is_error}
            steps.append(record)
            notify("tool", record)
            if state.plan != plan_before:
                tracer.log("plan", steps=state.plan)
                notify("plan", state.plan)
            if state.conclusion is not None and not had_conclusion:
                tracer.log("conclusion", conclusion=state.conclusion)
                notify("conclusion", state.conclusion)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    answer = None
    try:
        for round_no in range(1, max_steps + 1):
            msg = ask(round_no, tools)
            if not msg.tool_calls:
                if state.conclusion is None and not nudged:
                    # 模型想结束，但还没交结论：提醒一次
                    nudged = True
                    tracer.log("nudge", reason="no_conclusion")
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    messages.append({"role": "user", "content": "请先调用 submit_conclusion 提交结构化结论，然后再输出文字报告。"})
                    continue
                answer = msg.content
                break
            run_tool_calls(round_no, msg)
        else:
            # 步数用完：只给 submit_conclusion 这一个工具，让模型根据已有信息交结论
            stopped = True
            if state.conclusion is None:
                messages.append({"role": "user", "content": "已经达到最大步数。请根据目前查到的结果，调用 submit_conclusion 提交结论，"
                                                            "不确定的地方把 confidence 设为 low。"})
                try:
                    msg = ask(max_steps + 1, [SUBMIT_SPEC])
                    if msg.tool_calls:
                        run_tool_calls(max_steps + 1, msg)
                except Exception as e:
                    tracer.log("final_submit_error", error=repr(e))
            answer = f"超过最大步数（{max_steps} 轮），排查没有正常结束。"
    except Exception as e:
        tracer.log("run_error", error=repr(e))
        raise

    seconds = tracer.finish(answer or "", stopped)
    t = tracer.totals
    return {
        "answer": answer, "conclusion": state.conclusion, "plan": state.plan, "steps": steps,
        "usage": {"prompt_tokens": t["prompt_tokens"], "cached_tokens": t["cached_tokens"],
                  "completion_tokens": t["completion_tokens"]},
        "cost": tracer.cost(), "seconds": seconds, "run_id": tracer.run_id, "log_path": str(tracer.path),
        "stopped_by_limit": stopped, "nudged": nudged, "rejected": state.rejected,
    }

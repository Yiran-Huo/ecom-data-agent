"""
归因 Agent 主循环的测试：用「假模型」按剧本返回工具调用，不调用真实 API，也不需要数据库。

检查的是流程本身：
- 工具调用能正确执行，结果放回对话
- v3 的护栏：没写计划 / 没做剔除验证就交 concentrated 结论 → 被拒绝
- 模型忘了交结论就想结束 → 提醒一次
- 步数用完 → 强制只给 submit_conclusion
"""
import json

import pytest
from openai.types.chat import ChatCompletion

import agent.analysis.data as D
import agent.tracer as tracer_module
from agent.conclusion import validate
from agent.diagnose import run_diagnosis, tools_for
from synthetic import DAY, drop_purchases, make_frame

DATE = DAY.isoformat()


# ---------------------------------------------------------------------------
# 假模型
# ---------------------------------------------------------------------------
def _completion(message: dict) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "fake", "object": "chat.completion", "created": 0, "model": "fake",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    })


def call(*tools):
    """模型这一轮调用若干工具：call(("detect_anomaly", {...}), ...)"""
    return _completion({"content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
        for i, (n, a) in enumerate(tools)]})


def say(text):
    return _completion({"content": text})


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def with_options(self, **_):
        return self

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError("剧本用完了，模型被多调用了一次")
        return self.script.pop(0)


@pytest.fixture(scope="module")
def frame():
    base = make_frame()
    return drop_purchases(base, base["brand"] == "alpha", 0.7)


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path, frame):
    monkeypatch.setattr(D, "load_window", lambda day, database=None: frame)
    monkeypatch.setattr(tracer_module, "LOG_DIR", tmp_path)   # 日志写到临时目录


PLAN = ("update_plan", {"steps": [{"step": "确认异常", "status": "in_progress"},
                                  {"step": "拆因子", "status": "pending"},
                                  {"step": "定位维度", "status": "pending"},
                                  {"step": "剔除验证", "status": "pending"}]})
CONCLUSION = {
    "date": DATE, "is_anomaly": True, "direction": "down", "change_pct": -0.36, "main_factor": "P",
    "pattern": "concentrated", "confidence": "high", "hypotheses": ["alpha 可能断货"],
    "root_causes": [{"dimension": "brand", "value": "alpha", "factor": "CVR", "contribution_share": 0.9,
                     "path": [{"dimension": "brand", "value": "alpha"}]}],
}


def run(script, variant="v3", **kw):
    client = FakeClient(script)
    result = run_diagnosis(f"请排查 {DATE} 的 GMV", variant=variant, client=client, model="fake", **kw)
    return result, client


# ---------------------------------------------------------------------------
def test_v3_happy_path():
    result, client = run([
        call(PLAN),
        call(("detect_anomaly", {"date": DATE})),
        call(("decompose_gmv", {"date": DATE}), ("rank_dimensions", {"date": DATE})),
        call(("verify_exclusion", {"date": DATE, "exclude": [{"brand": "alpha"}]})),
        call(("submit_conclusion", CONCLUSION)),
        say("一句话结论：alpha 品牌购买大幅减少……"),
    ])
    assert result["conclusion"]["root_causes"][0]["value"] == "alpha"
    assert result["answer"].startswith("一句话结论")
    assert [s["tool"] for s in result["steps"]] == ["update_plan", "detect_anomaly", "decompose_gmv",
                                                   "rank_dimensions", "verify_exclusion", "submit_conclusion"]
    assert not any(s["is_error"] for s in result["steps"])
    assert "异常（下跌）" in result["steps"][1]["result"]
    assert len(result["plan"]) == 4 and result["rejected"] == 0
    # 工具结果确实放回了对话：最后一次请求里能看到 verify 的结果
    last_messages = client.requests[-1]["messages"]
    assert any(m.get("role") == "tool" and "不再异常" in m.get("content", "") for m in last_messages)


def test_v3_rejects_conclusion_without_verification():
    result, _ = run([
        call(PLAN),
        call(("submit_conclusion", CONCLUSION)),                                       # 没验证 → 拒绝
        call(("verify_exclusion", {"date": DATE, "exclude": [{"brand": "alpha"}]})),
        call(("submit_conclusion", CONCLUSION)),
        say("报告"),
    ])
    assert result["rejected"] == 1
    assert "剔除验证" in result["steps"][1]["result"] and result["steps"][1]["is_error"]
    assert result["conclusion"] is not None


def test_v3_rejects_conclusion_without_plan():
    result, _ = run([
        call(("submit_conclusion", {**CONCLUSION, "pattern": "broad", "root_causes": []})),
        call(PLAN),
        call(("submit_conclusion", {**CONCLUSION, "pattern": "broad", "root_causes": []})),
        say("报告"),
    ])
    assert result["rejected"] == 1 and result["conclusion"]["pattern"] == "broad"


def test_v2_has_no_guardrail_and_no_plan_tool():
    names = {t["function"]["name"] for t in tools_for("v2")}
    assert "update_plan" not in names and "detect_anomaly" in names
    result, _ = run([call(("submit_conclusion", CONCLUSION)), say("报告")], variant="v2")
    assert result["rejected"] == 0 and result["conclusion"] is not None


def test_v0_only_has_sql_tools():
    names = {t["function"]["name"] for t in tools_for("v0")}
    assert names == {"describe_table", "run_sql", "run_python", "submit_conclusion"}
    result, _ = run([call(("detect_anomaly", {"date": DATE})), call(("submit_conclusion", CONCLUSION)), say("报告")],
                    variant="v0")
    assert result["steps"][0]["is_error"] and "没有名为 detect_anomaly" in result["steps"][0]["result"]


def test_nudge_when_model_forgets_conclusion():
    result, client = run([
        say("我觉得是 alpha 的问题。"),                       # 想直接结束
        call(("submit_conclusion", CONCLUSION)),
        say("报告"),
    ], variant="v2")
    assert result["nudged"] and result["conclusion"] is not None
    sent = client.requests[-1]["messages"]   # 同一个对话列表，最后一次请求能看到全部历史
    assert any(m.get("role") == "user" and "请先调用 submit_conclusion" in m.get("content", "") for m in sent)


def test_bad_conclusion_format_is_rejected_with_reasons():
    bad = {**CONCLUSION, "pattern": "concentrated", "root_causes": [], "direction": "sideways"}
    result, _ = run([call(("submit_conclusion", bad)), call(("submit_conclusion", CONCLUSION)), say("报告")],
                    variant="v2")
    msg = result["steps"][0]["result"]
    assert "direction" in msg and "root_causes 不能为空" in msg and result["rejected"] == 1


def test_step_limit_forces_submit():
    loop = [call(("detect_anomaly", {"date": DATE}))] * 3
    result, client = run(loop + [call(("submit_conclusion", {**CONCLUSION, "confidence": "low"}))], max_steps=3)
    assert result["stopped_by_limit"]
    assert [t["function"]["name"] for t in client.requests[-1]["tools"]] == ["submit_conclusion"]
    # v3 下没有计划会被拒绝，所以结论为空 —— 这是期望的行为（评测会记为没有结论）
    assert result["conclusion"] is None and result["rejected"] == 1


def test_tool_errors_are_returned_as_text():
    result, _ = run([
        call(("drill_down", {"date": DATE, "dimension": "country"}),
             ("detect_anomaly", {"date": "2019-13-45"}),
             ("drill_down", {"date": DATE})),
        call(("submit_conclusion", CONCLUSION)), say("报告"),
    ], variant="v2")
    errors = [s["result"] for s in result["steps"][:3]]
    assert all(e.startswith("错误") for e in errors)


def test_validate_normalizes():
    out, errors = validate({**CONCLUSION, "main_factor": "cvr", "pattern": "Concentrated"})
    assert not errors and out["main_factor"] == "CVR" and out["pattern"] == "concentrated"
    _, errors = validate({"date": DATE})
    assert errors


def test_network_error_detection():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
    from run_diagnosis_eval import is_network_error
    assert is_network_error("APIConnectionError('Connection error.')")
    assert is_network_error("APITimeoutError('Request timed out.')")
    assert not is_network_error("")
    assert not is_network_error("ValueError('bad json')")

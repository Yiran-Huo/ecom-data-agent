"""
运行日志：每次 run_agent 都会在 logs/runs/ 下生成一个 .jsonl 文件，一行记录一个事件。

    run_start   问题、模型、开始时间
    llm_call    第几轮、耗时、token 用量（含缓存命中）
    tool_call   工具名、参数、结果预览、是否出错、耗时
    run_end     最终回答、总轮数、总 token、总耗时、估算费用

为什么用 JSONL（每行一个 JSON）：程序中途崩溃，前面写进去的行也不会丢；
分析时用 pd.read_json(路径, lines=True) 就能直接读成表格。

费用估算需要在 .env 里填价格（每百万 token 的价格，单位自定，比如人民币元），不填就不算：
    LLM_PRICE_INPUT=       普通输入
    LLM_PRICE_INPUT_CACHED= 命中缓存的输入（不填则按普通输入算）
    LLM_PRICE_OUTPUT=      输出
"""
import json
import os
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs" / "runs"


def _price(name):
    v = os.getenv(name)
    return float(v) if v else None


class Tracer:
    def __init__(self, question: str, model: str, tags: dict | None = None):
        self.run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.path = LOG_DIR / f"{self.run_id}.jsonl"
        self.start = time.time()
        self.totals = {"llm_calls": 0, "tool_calls": 0, "tool_errors": 0,
                       "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log("run_start", question=question, model=model, tags=tags or {})

    # 写一行
    def log(self, event: str, **fields):
        record = {"run_id": self.run_id, "event": event,
                  "t": round(time.time() - self.start, 3), **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")   # ensure_ascii=False：中文原样写入

    def llm_call(self, round_no: int, latency: float, usage):
        """记录一次模型调用。usage 是 openai 返回的 resp.usage（可能为空）。"""
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        # 缓存命中的 token：DeepSeek 用 prompt_cache_hit_tokens，OpenAI 用 prompt_tokens_details.cached_tokens
        cached = getattr(usage, "prompt_cache_hit_tokens", None)
        if cached is None:
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) if details else 0
        cached = cached or 0

        self.totals["llm_calls"] += 1
        self.totals["prompt_tokens"] += prompt
        self.totals["cached_tokens"] += cached
        self.totals["completion_tokens"] += completion
        self.log("llm_call", round=round_no, latency_s=round(latency, 2),
                 prompt_tokens=prompt, cached_tokens=cached, completion_tokens=completion)

    def tool_call(self, round_no: int, tool: str, arguments: str, result: str, is_error: bool, latency: float):
        self.totals["tool_calls"] += 1
        self.totals["tool_errors"] += int(is_error)
        self.log("tool_call", round=round_no, tool=tool, arguments=arguments,
                 result_preview=result[:500], result_chars=len(result),
                 is_error=is_error, latency_s=round(latency, 2))

    def cost(self):
        """按 .env 里的价格估算费用；没填价格就返回 None。"""
        p_in, p_out = _price("LLM_PRICE_INPUT"), _price("LLM_PRICE_OUTPUT")
        if p_in is None or p_out is None:
            return None
        p_cached = _price("LLM_PRICE_INPUT_CACHED") or p_in
        t = self.totals
        uncached = t["prompt_tokens"] - t["cached_tokens"]
        return round((uncached * p_in + t["cached_tokens"] * p_cached + t["completion_tokens"] * p_out) / 1e6, 6)

    def finish(self, answer: str, stopped_by_limit: bool):
        seconds = round(time.time() - self.start, 2)
        self.log("run_end", answer=answer, stopped_by_limit=stopped_by_limit,
                 seconds=seconds, cost=self.cost(), **self.totals)
        return seconds

"""
电商数据 MCP Server：把 Agent 的数据工具按 MCP 标准暴露出去，
让 Claude 桌面版、Cursor 等任何支持 MCP 的客户端都能直接分析这个数据库。

提供的内容：
    工具（Tools）
        list_tables      列出所有表和说明
        describe_table   查看表结构、字段注释、样例
        run_sql          执行只读 SQL（最多返回 50 行）
        run_python       运行 Python 画图 / 计算（默认关闭，需要设置环境变量 ECOM_MCP_ENABLE_PYTHON=1）
    资源（Resources）
        ecom://schema-notes   数据说明书：表关系、指标口径、数据坑
        ecom://data-profile   自动生成的数据概况
    提示词（Prompts）
        analyze          带上分析规则和数据说明书的提问模板

安全护栏和 Agent 完全一样：只读数据库账号、只允许查询语句、15 秒超时、结果截断。
每次工具调用都记录到 logs/mcp_calls.jsonl。

运行方式（一般不需要手动运行，由 MCP 客户端自动启动）：
    python mcp_server/server.py
自测：
    python scripts/mcp_smoke_test.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # 让客户端从任何目录启动时都能 import agent.xxx

from mcp.server.mcpserver import Image, MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from agent import tools as T  # noqa: E402
from agent.prompts import ROLE_AND_RULES  # noqa: E402

ENABLE_PYTHON = os.getenv("ECOM_MCP_ENABLE_PYTHON") == "1"
CALL_LOG = ROOT / "logs" / "mcp_calls.jsonl"

server = MCPServer(
    name="ecom-data",
    title="电商行为数据分析",
    version="0.1.0",
    instructions=(
        "这是一个电商用户行为数据库（REES46 数据集，2019 年 10 月，按用户 1/10 抽样，MySQL 8）。"
        "分析前先读取资源 ecom://schema-notes，了解表关系、指标口径和数据坑。"
        "要点：金额类指标排除 price <= 0；用户数用 COUNT(DISTINCT user_id)；"
        "空品牌、空品类不参与排名；比率排名只看分母 ≥ 1000 的项目；绝对数是样本值（约全站 1/10）。"
    ),
)

# 告诉客户端：这些工具只读、可重复调用、不访问外部网络（客户端可以据此决定是否需要用户确认）
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _log(tool: str, args: dict, result: str, started: float):
    """记录每一次 MCP 工具调用，方便排查外部客户端到底调了什么。"""
    CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool, "args": args,
        "is_error": result.startswith(("错误", "SQL 错误")) or "运行出错" in result,
        "latency_s": round(time.time() - started, 2), "result_preview": result[:300],
    }
    with CALL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 工具：直接复用 agent/tools.py，护栏完全相同
# ---------------------------------------------------------------------------
@server.tool(annotations=READ_ONLY)
def list_tables() -> str:
    """列出数据库里所有的表及其说明。不知道有哪些表时先调用它。"""
    t0 = time.time()
    result = T.run_sql(
        "SELECT table_name AS `表`, table_comment AS `说明` FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY table_name"
    )
    _log("list_tables", {}, result, t0)
    return result


@server.tool(annotations=READ_ONLY)
def describe_table(table: str) -> str:
    """查看一张表的说明、字段类型、字段注释和 3 行样例。可选的表：events、products、categories、sessions。"""
    t0 = time.time()
    result = T.describe_table(table)
    _log("describe_table", {"table": table}, result, t0)
    return result


@server.tool(annotations=READ_ONLY)
def run_sql(query: str) -> str:
    """在 MySQL 8 数据库上执行一条只读 SQL（SELECT / WITH / SHOW / DESCRIBE / EXPLAIN），
    返回 Markdown 表格，最多 50 行，单条查询限时 15 秒。写操作会被拒绝。"""
    t0 = time.time()
    result = T.run_sql(query)
    _log("run_sql", {"query": query}, result, t0)
    return result


if ENABLE_PYTHON:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    def run_python(code: str) -> list:
        """在独立进程中运行 Python（限时 60 秒），用于画图或复杂计算。
        已准备好 pd、plt 和只读数据库连接 engine（用 pd.read_sql(sql, engine) 取数）。
        图片请用 plt.savefig('outputs/英文文件名.png') 保存，会作为图片返回。"""
        t0 = time.time()
        result = T.run_python(code)
        _log("run_python", {"code": code[:500]}, result, t0)
        contents: list = [result]
        for chart in T.find_charts(result):          # 生成的图片直接作为图片内容返回给客户端
            contents.append(Image(path=ROOT / chart))
        return contents


# ---------------------------------------------------------------------------
# 资源：客户端可以直接读取的背景资料
# ---------------------------------------------------------------------------
@server.resource("ecom://schema-notes", name="数据说明书", mime_type="text/markdown",
                 description="表结构与关联、指标口径、数据坑、MySQL 写法注意事项")
def schema_notes() -> str:
    return (ROOT / "agent" / "schema_notes.md").read_text(encoding="utf-8")


@server.resource("ecom://data-profile", name="数据概况", mime_type="text/markdown",
                 description="自动生成的数据规模、关键指标参考值、数据质量统计")
def data_profile() -> str:
    path = ROOT / "agent" / "data_profile.md"
    return path.read_text(encoding="utf-8") if path.exists() else "（还没有生成，先运行 scripts/verify_db.py）"


# ---------------------------------------------------------------------------
# 提示词：在客户端里一键使用的分析模板
# ---------------------------------------------------------------------------
@server.prompt(title="电商数据分析")
def analyze(question: str) -> str:
    """按项目的分析规则和数据口径来回答一个业务问题。"""
    notes = (ROOT / "agent" / "schema_notes.md").read_text(encoding="utf-8")
    return (f"{ROLE_AND_RULES}\n\n# 数据说明书\n\n{notes}\n\n"
            f"请使用 ecom-data 的工具查询数据库，回答下面的问题：\n{question}")


if __name__ == "__main__":
    server.run("stdio")   # 通过标准输入输出和客户端通信（stdout 只能用来传协议消息，不能 print）

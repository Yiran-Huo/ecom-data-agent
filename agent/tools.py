"""
Agent 的工具，以及给模型看的「工具说明书」。

    describe_table(table)  看一张表有哪些字段、字段注释、3 行样例
    run_sql(query)         执行一条只读 SQL，返回最多 50 行
    run_python(code)       在独立进程里运行 Python（pandas / matplotlib），用来计算和画图
    save_memory(note)      保存一条用户偏好到长期记忆（实现在 memory.py）

所有工具都遵守同一个原则：输入是普通参数，返回一段文字；出错时返回错误说明，不抛异常。
这样模型能看到错在哪里，自己改正。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from agent.db import engine
from agent.memory import save_note

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "outputs"

TABLES = {"events", "products", "categories", "sessions"}   # describe_table 的白名单
MAX_ROWS = 50            # run_sql 最多返回的行数
SQL_TIMEOUT_MS = 15_000  # 单条 SQL 最长 15 秒
PY_TIMEOUT_S = 60        # run_python 最长 60 秒
MAX_OUTPUT_CHARS = 3000  # 工具返回的文字最多这么长，防止撑爆上下文


# ---------------------------------------------------------------------------
# 小工具函数
# ---------------------------------------------------------------------------
def _query_df(sql: str, limit: int | None = None) -> pd.DataFrame:
    """执行 SQL 并返回 DataFrame。limit 不为空时最多只取 limit 行（不会把整张表拉进内存）。"""
    with engine.connect() as conn:
        conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {SQL_TIMEOUT_MS}"))
        result = conn.execute(text(sql))
        rows = result.fetchmany(limit) if limit else result.fetchall()
        return pd.DataFrame(rows, columns=list(result.keys()))


def _fmt_number(v):
    """小数显示成完整的普通写法（4727987.34、0.1153），不用科学计数法 4.72799e+06；空值显示为 NULL。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "NULL"   # 明确告诉模型这里是空值，而不是留一个空格子
    if isinstance(v, (float, Decimal)):
        return f"{float(v):.4f}".rstrip("0").rstrip(".")
    return v


def _to_table(df: pd.DataFrame) -> str:
    """DataFrame → Markdown 表格文字。数字保留完整精度，避免模型拿到被四舍五入的近似值。"""
    df = df.apply(lambda col: col.map(_fmt_number))
    # disable_numparse：已经格式化好的数字字符串，不让 tabulate 再转回科学计数法
    return df.to_markdown(index=False, disable_numparse=True)


def _truncate(s: str, n: int = MAX_OUTPUT_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n…（输出过长，已截断，原长 {len(s)} 字符）"


# ---------------------------------------------------------------------------
# 工具 1：查看表结构
# ---------------------------------------------------------------------------
def describe_table(table: str) -> str:
    """输入表名，返回：表说明、每个字段的类型和注释、3 行样例数据。"""
    table = table.strip().strip("`")
    if table not in TABLES:
        # 表名会被直接拼进 SQL，所以必须先和白名单比对，防止注入
        return f"错误：没有表 {table}。可用的表：{', '.join(sorted(TABLES))}"
    try:
        comment = _query_df(
            "SELECT table_comment FROM information_schema.tables "
            f"WHERE table_schema = DATABASE() AND table_name = '{table}'"
        ).iat[0, 0]
        cols = _query_df(f"SHOW FULL COLUMNS FROM {table}")[["Field", "Type", "Null", "Comment"]]
        sample = _query_df(f"SELECT * FROM {table} LIMIT 3")
    except Exception as e:
        return f"错误：{getattr(e, 'orig', e)}"
    return (
        f"表 {table}：{comment}\n\n字段：\n{_to_table(cols)}"
        f"\n\n样例（3 行）：\n{_to_table(sample)}"
    )


# ---------------------------------------------------------------------------
# 工具 2：执行 SQL
# ---------------------------------------------------------------------------
READ_ONLY_STARTS = ("select", "with", "show", "describe", "desc", "explain")


def run_sql(query: str) -> str:
    """输入一条 SQL，返回结果表（最多 50 行）。只允许只读语句。"""
    q = query.strip().rstrip(";").strip()
    first_word = q.split(None, 1)[0].lower() if q else ""

    # 第一层保险：只允许查询类语句，且一次只能一条（第二层保险是数据库只读账号）
    if first_word not in READ_ONLY_STARTS:
        return "错误：只允许只读查询（SELECT / WITH / SHOW / DESCRIBE / EXPLAIN）"
    if ";" in q:
        return "错误：一次只能执行一条语句，请去掉中间的分号"

    try:
        df = _query_df(q, limit=MAX_ROWS + 1)   # 多取一行，用来判断有没有被截断
    except Exception as e:
        msg = str(getattr(e, "orig", e))
        if "3024" in msg:   # MySQL 的超时错误码
            msg += "\n提示：查询超时。请缩小时间范围、先聚合再 join，或利用索引字段过滤。"
        return f"SQL 错误：{msg}"

    if df.empty:
        return "（查询结果为空）"
    out = _to_table(df.head(MAX_ROWS))
    if len(df) > MAX_ROWS:
        out += f"\n（结果超过 {MAX_ROWS} 行，只显示前 {MAX_ROWS} 行。请用聚合、ORDER BY + LIMIT 缩小结果）"
    return _truncate(out)


# ---------------------------------------------------------------------------
# 工具 3：运行 Python（计算、画图）
# ---------------------------------------------------------------------------
# 每段代码运行前都会先执行这段「准备代码」，所以模型的代码里可以直接用 pd、plt、engine
PREAMBLE = """
import warnings; warnings.filterwarnings("ignore")
import pandas as pd
import matplotlib
matplotlib.use("Agg")                       # 不弹窗，只保存图片
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti TC", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False  # 中文字体下正常显示负号
plt.rcParams["axes.formatter.limits"] = (-7, 9)   # 坐标轴不要用科学计数法（避免出现 1e6）
from agent.db import engine                 # 同样是只读账号
# ---- 以下是模型写的代码 ----
"""


def run_python(code: str) -> str:
    """输入一段 Python 代码，在独立进程里运行，返回打印输出和新生成的图片路径。"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    before = set(OUTPUT_DIR.glob("*.png"))

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(PREAMBLE + code)
        script = f.name

    env = {**os.environ, "PYTHONPATH": str(ROOT)}   # 让子进程能 import agent.db
    try:
        proc = subprocess.run(
            [sys.executable, script],
            cwd=ROOT,                 # 相对路径 outputs/xxx.png 以项目根目录为准
            env=env,
            capture_output=True,
            text=True,
            timeout=PY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"错误：代码运行超过 {PY_TIMEOUT_S} 秒，已被终止。请减少数据量，或先在 SQL 里聚合。"
    finally:
        os.unlink(script)

    parts = []
    if proc.stdout.strip():
        parts.append("输出：\n" + _truncate(proc.stdout.strip()))
    if proc.returncode != 0:
        err_tail = "\n".join(proc.stderr.strip().splitlines()[-8:])   # 只看报错的最后几行
        parts.append("运行出错：\n" + err_tail)
    new_charts = sorted(set(OUTPUT_DIR.glob("*.png")) - before)
    if new_charts:
        parts.append("生成图片：" + ", ".join(str(p.relative_to(ROOT)) for p in new_charts))
    return "\n\n".join(parts) or "（代码运行成功，没有输出。需要看结果请 print）"


# ---------------------------------------------------------------------------
# 给模型看的工具说明书（模型看不到上面的代码，只能看到这里）
# ---------------------------------------------------------------------------
def _spec(name, description, param, param_desc):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {param: {"type": "string", "description": param_desc}},
                "required": [param],
            },
        },
    }


TOOL_SPECS = [
    _spec(
        "describe_table",
        "查看一张表的说明、字段类型、字段注释和 3 行样例。写 SQL 前不确定字段含义时先调用。",
        "table", f"表名，可选：{', '.join(sorted(TABLES))}",
    ),
    _spec(
        "run_sql",
        "在 MySQL 8 数据库 ecom 上执行一条只读 SQL，返回结果表（最多 50 行）。用于取数和聚合计算。",
        "query", "一条 MySQL 8 语法的 SELECT / WITH 语句",
    ),
    _spec(
        "run_python",
        "在独立进程中运行 Python，用于画图或 SQL 不方便做的计算。pd、plt 和只读数据库连接 engine 已经准备好，"
        "直接用 pd.read_sql(sql, engine) 取数；不要自己 import engine、不要 create_engine。"
        "图片必须用 plt.savefig('outputs/英文文件名.png', dpi=150, bbox_inches='tight') 保存；"
        "需要看结果请 print。限时 60 秒。",
        "code", "完整的 Python 代码",
    ),
]

# 长期记忆工具：只在正常对话时提供，评测时不提供（保证评测可复现）
MEMORY_SPEC = _spec(
    "save_memory",
    "保存一条用户明确要求记住的偏好（例如「以后金额用万为单位」），下次对话依然有效。只在用户明确要求记住时调用。",
    "note", "要记住的内容，一句话",
)

TOOLS = {"describe_table": describe_table, "run_sql": run_sql, "run_python": run_python, "save_memory": save_note}


def call_tool(name: str, arguments: str) -> str:
    """按名字调用工具。arguments 是模型给的 JSON 字符串。任何问题都返回文字，不抛异常。"""
    if name not in TOOLS:
        return f"错误：没有名为 {name} 的工具，可用的工具有：{', '.join(TOOLS)}"
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return "错误：工具参数不是合法的 JSON"
    try:
        return TOOLS[name](**args)
    except TypeError as e:   # 参数名不对，例如把 query 写成了 sql
        return f"错误：参数不对（{e}）"


def find_charts(tool_result: str) -> list[str]:
    """从 run_python 的返回文字里找出生成的图片路径。"""
    return re.findall(r"outputs/[\w\-.]+\.png", tool_result)

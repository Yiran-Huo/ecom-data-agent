"""
工具的单元测试：绕过模型，直接测试三个工具和它们的安全护栏。
需要数据库已经导入数据。运行方式（在项目根目录）：pytest -q
"""
import re

from agent.tools import ROOT, call_tool, describe_table, run_python, run_sql


# ---------------- run_sql ----------------
def test_run_sql_counts_rows():
    out = run_sql("SELECT COUNT(*) AS n FROM categories")
    assert re.search(r"\d+", out) and "错误" not in out


def test_run_sql_rejects_writes():
    for sql in ["DROP TABLE events", "DELETE FROM events", "UPDATE products SET brand = 'x'",
                "INSERT INTO categories VALUES (1, 'a', 'a', 'a')"]:
        assert run_sql(sql).startswith("错误")


def test_run_sql_rejects_multiple_statements():
    assert run_sql("SELECT 1; DROP TABLE events").startswith("错误")


def test_run_sql_truncates_large_results():
    out = run_sql("SELECT event_id FROM events")
    assert "只显示前 50 行" in out


def test_run_sql_returns_error_text_instead_of_raising():
    out = run_sql("SELECT * FROM orders")
    assert out.startswith("SQL 错误") and "orders" in out


# ---------------- describe_table ----------------
def test_describe_table_shows_comments_and_samples():
    out = describe_table("products")
    assert "brand" in out and "样例" in out


def test_describe_table_whitelist():
    assert describe_table("mysql.user").startswith("错误")


# ---------------- run_python ----------------
def test_run_python_prints_output():
    assert "42" in run_python("print(6 * 7)")


def test_run_python_reads_database():
    out = run_python("print(pd.read_sql('SELECT COUNT(*) AS n FROM categories', engine)['n'][0])")
    assert re.search(r"\d+", out) and "出错" not in out


def test_run_python_reports_errors():
    assert "运行出错" in run_python("1 / 0")


def test_run_python_saves_chart():
    out = run_python("plt.plot([1, 2, 3]); plt.savefig('outputs/_pytest_chart.png')")
    assert "outputs/_pytest_chart.png" in out
    (ROOT / "outputs" / "_pytest_chart.png").unlink()   # 测试完删掉


# ---------------- call_tool ----------------
def test_call_tool_unknown_tool():
    assert call_tool("drop_everything", "{}").startswith("错误")


def test_call_tool_bad_json():
    assert call_tool("run_sql", "{not json").startswith("错误")


def test_call_tool_wrong_argument_name():
    assert call_tool("run_sql", '{"sql": "SELECT 1"}').startswith("错误")

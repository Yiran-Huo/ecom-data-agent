"""
MCP Server 自测：像真正的 MCP 客户端一样，启动 server 并调用它，确认一切正常。
不需要 Claude 桌面版，也不调用大模型。

运行方式（在项目根目录）：python scripts/mcp_smoke_test.py
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server" / "server.py"


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", f"<{type(c).__name__}>") for c in result.content)


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=[str(SERVER)],
        env={**os.environ, "ECOM_MCP_ENABLE_PYTHON": "1"},   # 自测时把 run_python 也打开
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        print("工具：", [t.name for t in tools.tools])
        resources = await client.list_resources()
        print("资源：", [str(r.uri) for r in resources.resources])
        prompts = await client.list_prompts()
        print("提示词：", [p.name for p in prompts.prompts])

        checks = []

        r = await client.call_tool("list_tables", {})
        checks.append(("list_tables 列出 4 张表", all(t in text_of(r) for t in ["events", "products", "categories", "sessions"])))

        r = await client.call_tool("run_sql", {"query": "SELECT COUNT(*) AS n FROM categories"})
        checks.append(("run_sql 能查询", "错误" not in text_of(r)))

        r = await client.call_tool("run_sql", {"query": "DROP TABLE events"})
        checks.append(("run_sql 拒绝写操作", text_of(r).startswith("错误")))

        r = await client.call_tool("describe_table", {"table": "mysql.user"})
        checks.append(("describe_table 拒绝白名单外的表", text_of(r).startswith("错误")))

        r = await client.read_resource("ecom://schema-notes")
        checks.append(("能读取数据说明书", "数据坑" in r.contents[0].text))

        r = await client.get_prompt("analyze", {"question": "GMV 最高的品牌是哪个？"})
        checks.append(("analyze 提示词包含规则和问题", "GMV 最高的品牌" in r.messages[0].content.text))

        r = await client.call_tool("run_python", {"code": "plt.plot([1, 2, 3]); plt.savefig('outputs/_mcp_test.png'); print('ok')"})
        checks.append(("run_python 返回图片", any(type(c).__name__ == "ImageContent" for c in r.content)))
        (ROOT / "outputs" / "_mcp_test.png").unlink(missing_ok=True)

    print()
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")
    if not all(ok for _, ok in checks):
        sys.exit("\n有检查没通过")
    print("\n✅ MCP Server 工作正常，可以接入客户端了")


if __name__ == "__main__":
    asyncio.run(main())

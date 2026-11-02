"""
第三方 MCP server 最小示例（P2-S7 演示）：证明 P2 agent 的工具集可即插即用扩展。
启动：python examples/mcp_demo_server.py --http 8610
接入 agent：enable_mcp_tools([{"name": "demo", "transport": "http", "url": "http://127.0.0.1:8610/mcp"}])
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("p2-demo")


@mcp.tool()
def now_utc() -> str:
    """返回当前 UTC 时间（ISO 格式）。第三方工具示例：与数据库无关的纯演示能力。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = int(sys.argv[sys.argv.index("--http") + 1])
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")

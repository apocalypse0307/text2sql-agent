"""
P2 四工具的 MCP Server（P2-S7）：把 Text2SQL 能力以标准 MCP 协议暴露给任何第三方客户端。

设计立场：
  - 复用 > 重写：四个工具直接调 src/tools/registry 的同一实现（含四层安全），
    MCP 只是「协议外壳」——通过 MCP 进来的 execute_readonly_sql 依然走
    只读账号 + 表白名单 + LIMIT + 超时，**不因换协议而旁路安全层**。
  - 两种传输：stdio（Claude Desktop / Cursor 等桌面客户端标配）+
    streamable-http（远程/服务间调用，本仓库自建 client 用）。
  - data_ref 适配：registry 的 compute_metric/render_chart 依赖进程内 results_registry
    （data_ref="result" 是图内概念）；MCP 是无状态协议，改为调用方直接传 rows。

启动：
  stdio:   python src/mcp_server.py                 （子进程被客户端拉起，stdin/stdout 通信）
  http:    python src/mcp_server.py --http 8600     （streamable-http，端点 http://127.0.0.1:8600/mcp）

启动期体检（P2 加固，2026-10-18）：
  缺凭据是**部署错误**，必须在启动时暴露，而不是等客户端第一次调 generate_sql 才炸
  （那时现象是「工具好像坏了」，很难归因）。体检写在 **build_server() 工厂**里而不是
  `__main__` 里——__main__ 只保护那一个入口，`mcp dev` / 被 import 复用都绕得过去；
  工厂则让**任何拿到 server 的路径**都必须经过体检（保护依赖代码结构，不依赖入口点）。
    · CLI 入口（`python src/mcp_server.py`）：默认硬失败（stderr 写「缺哪个变量 + 三步修法」
      → `sys.exit(1)`）；`--allow-degraded` 则 WARN 并**不注册 generate_sql**。
    · import 入口（`mcp dev` / `from src.mcp_server import mcp`）：**fail-closed** ——
      不 sys.exit（否则 `python -c "import src.mcp_server"` 会被直接杀掉），但不暴露
      generate_sql（没有凭据就不给用不了的工具）。
  ★ stdout 纪律：stdio 模式下 stdout 是**协议信道**，一个字节都不能脏。所有诊断/日志一律
    走 stderr。`tests/test_mcp.py` 有一条用例逐行断言 stdout 全是合法 JSON-RPC 帧。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP

# FastMCP 自己的 Settings 里有个未解析的前向引用（`lifespan`），pydantic-settings 每次
# 起服务都会往 stderr 吐一条 IncompleteFieldDefinitionWarning。它不影响本项目任何行为
# （我们不用 lifespan），但会把「缺凭据」这类**部署报错**挤到警告后面，部署脚本 grep 时
# 第一眼看到的是噪声。只过滤这一个类别，其他 pydantic_settings 警告照常输出。
try:                                                    # pragma: no cover
    import warnings

    from pydantic_settings.sources.utils import IncompleteFieldDefinitionWarning

    warnings.filterwarnings("ignore", category=IncompleteFieldDefinitionWarning)
except Exception:
    pass

from src.core.llm import MissingCredentialError, require_llm_credentials
from src.tools.registry import (
    _tool_execute_sql,
    _tool_generate_sql,
    _tool_compute_metric,
    _tool_render_chart,
)

# ---- 四个工具的实现（纯函数，不在这里注册：注册统一由 build_server 负责）----

def execute_readonly_sql(sql: str) -> str:
    """四层安全校验（只读/表白名单/LIMIT/超时）后执行 SELECT，返回结果行。"""
    out = _tool_execute_sql({"sql": sql})
    return json.dumps(out, ensure_ascii=False, default=str)


def compute_metric(metric: str, rows: list[dict], value_col: str,
                   group_by: list[str] | None = None) -> str:
    """对结果行做二次指标：sum/avg/count/max/min/mom/yoy/ratio（rows 为 execute 返回的结果行）。"""
    out = _tool_compute_metric(
        {"metric": metric, "data_ref": "result", "value_col": value_col,
         "group_by": group_by or []},
        {"result": rows})
    return json.dumps(out, ensure_ascii=False, default=str)


def render_chart(chart_type: str, rows: list[dict], x: str, y: str) -> str:
    """生成 ECharts 图表配置（bar/line/pie）。rows 为结果行，x/y 为列名。"""
    out = _tool_render_chart(
        {"chart_type": chart_type, "x": x, "y": y, "data_ref": "result"},
        {"result": rows})
    return json.dumps(out, ensure_ascii=False, default=str)


# generate_sql 依赖 LLM 凭据，**是否暴露由 build_server 的体检决定**；
# 缺凭据时它根本不出现在 tools/list 里（fail-closed）。
def generate_sql(question: str, tables: list[str] | None = None) -> str:
    """把业务问题转成只读 SELECT SQL（SQLite 方言，内部自动注入 Schema 与 few-shot）。"""
    out = _tool_generate_sql({"question": question, "tables": tables or []})
    return json.dumps(out, ensure_ascii=False, default=str)


def _llm_tool_allowed(*, allow_degraded: bool, quiet: bool = False) -> bool:
    """体检：允许暴露 generate_sql 吗？不允许且不允许降级 → 抛 MissingCredentialError。

    诊断一律走 stderr：stdio 模式下 stdout 是协议信道，脏一个字节客户端就断连。
    """
    try:
        require_llm_credentials()
        return True
    except MissingCredentialError as exc:
        if not allow_degraded:
            raise
        if not quiet:
            first_line = str(exc).splitlines()[0].strip()
            print(f"[WARN] 降级：generate_sql 未暴露（{first_line}）", file=sys.stderr)
        return False


def build_server(*, allow_degraded: bool = False, quiet: bool = False) -> FastMCP:
    """**唯一的 server 构造入口**：先体检，再按结果决定要不要注册 generate_sql。

    为什么要有这个工厂（而不是在 `if __name__ == "__main__"` 里体检 + 注册）：
    __main__ 里的逻辑只保护**那一个入口**，换个方式加载就绕过了——
      ① `python src/mcp_server.py`          → 走 __main__
      ② `mcp dev src/mcp_server.py`         → import 模块、复用模块级 `mcp` 对象
      ③ 被别的代码 `from src.mcp_server import mcp` 复用
    把体检挪进工厂后，**任何拿到 server 的路径都必须经过它**：保护依赖代码结构，
    而不是依赖某个入口点被人记得调用。
    """
    server = FastMCP("p2-text2sql",
                     instructions="Olist 电商数据的 Text2SQL 查询工具集（只读，四层安全）")
    server.tool()(execute_readonly_sql)        # 这 3 个不需要 LLM，无条件注册
    server.tool()(compute_metric)
    server.tool()(render_chart)
    if _llm_tool_allowed(allow_degraded=allow_degraded, quiet=quiet):
        server.tool()(generate_sql)            # 条件注册：缺凭据时它根本不出现在 tools/list
    return server


# 模块级对象：给 ②③ 这类**以 import 方式加载**的入口用。
# 这里刻意 `allow_degraded=True` + `quiet=True`，理由：
#   · import 期不能 sys.exit——那会让 `python -c "import src.mcp_server"` 直接杀进程；
#   · 降级方向是 **fail-closed**（没有凭据就不暴露 LLM 工具），不是带病暴露；
#   · 静默只是不打扰 import 者，CLI 入口（__main__）仍会硬失败并把话说清楚。
mcp = build_server(allow_degraded=True, quiet=True)


if __name__ == "__main__":
    # CLI 入口：部署错误必须硬失败。体检失败 → stderr 写「缺哪个变量 + 三步修法」→ exit 1
    try:
        _server = build_server(allow_degraded="--allow-degraded" in sys.argv)
    except MissingCredentialError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    if "--http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http") + 1]) if len(sys.argv) > sys.argv.index("--http") + 1 else 8600
        _server.settings.host = "127.0.0.1"
        _server.settings.port = port
        _server.run(transport="streamable-http")
    else:
        _server.run(transport="stdio")

"""
MCP Client（P2-S7）：动态发现外部 MCP server 的工具并注册进 agent 工具集。

架构：
  - 「工具即插即用」：agent 的工具集不再写死——启动时连上 MCP server，list_tools
    发现能力，转成 OpenAI function-calling schema 注册进 registry，下一轮
    bind_tools 模型就能看见并调用。接一个新数据工具 = 改一行配置，不改图。
  - **外部工具不旁路安全**：注册走 registry.register_external_tool——参数先过
    Pydantic（由 MCP inputSchema 动态生成）再转发；同名原生工具优先不覆盖。
  - 同步桥：agent 图的 tools 节点是同步 dispatch，而 MCP client 是 async——
    用后台线程跑事件循环，sync 调用经 run_coroutine_threadsafe 桥接，
    会话（stdio 子进程 / http 连接）常驻不反复建连。

用法：
    from src.mcp_client import enable_mcp_tools
    registered = enable_mcp_tools([
        {"name": "p2", "transport": "stdio", "command": sys.executable,
         "args": ["src/mcp_server.py"]},
        {"name": "remote", "transport": "http", "url": "http://127.0.0.1:8600/mcp"},
    ])
    # registered = 发现并注册的工具名列表；此后 dispatch() 可直接调它们
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from src.core.llm import MissingCredentialError, require_llm_credentials
from src.tools.registry import register_external_tool

log = logging.getLogger(__name__)

# MCP stdio 子进程的环境**不是**父进程 os.environ 的复制：SDK 只透传
# get_default_environment() 的白名单（HOME/PATH/TERM… 本机实测 12 个变量）。
# 于是「父进程明明有 DEEPSEEK_API_KEY，子进程却报 Missing credentials」——
# 根因在传参层，不在密钥配置层。
#
# 修法刻意**不做 `**os.environ` 全量透传**：那会把父进程里所有密钥（数据库口令、
# 云厂商 token、第三方 API key）一并塞进一个我们并未审计过全部代码的子进程，
# 等于用一个「修好了」换一个「泄露面无限大」。正确姿势是显式白名单。
#
# ★ 取值源是 **settings 而不是 os.environ**（本版修正）：
#   settings 由 pydantic-settings 合并「.env 文件 + 进程环境变量」得到，是项目唯一
#   配置入口。只读 os.environ 会漏掉**只写在 .env 里**的值——AGNES 时代能跑纯属巧合
#   （子进程自己也读了 CWD 的 .env 兜底），换个 provider 就露馅。
#   改从 settings 取之后还顺带得到两个性质：
#     ① 子进程**不再依赖 CWD 能找到 .env**（从任意目录启动 mcp_server.py 都能跑）；
#     ② 配置来源唯一，测试可以用 monkeypatch 直接伪造 settings 字段，
#        不必去改真实环境变量（见 tests/test_mcp.py 的「不依赖环境的用例」）。
#
# 收录判据：子进程启动后为「复现父进程的 LLM 与数据访问」所必需。
#   · 评测资产路径（goldset/fewshots）不在其中——那不是运行时必需；
#   · 写库口令 agent_rw 不在其中——子进程只做只读查询；
#   · SCENARIO 已移除：settings 里没有该字段、get_profile() 目前也忽略它，
#     留在白名单里只会让人误以为「换场景已经接线」。
_MCP_ENV_FROM_SETTINGS: dict[str, str] = {
    # —— LLM 选型与凭据（AGNES_API_KEY / DEEPSEEK_API_KEY 是唯一的机密字段）——
    "LLM_PROVIDER": "llm_provider",
    "LLM_MODEL": "llm_model",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "DEEPSEEK_BASE_URL": "deepseek_base_url",
    "DEEPSEEK_THINKING": "deepseek_thinking",
    "AGNES_API_KEY": "agnes_api_key",
    "AGNES_BASE_URL": "agnes_base_url",
    "AGNES_MODEL": "agnes_model",
    # —— LLM 行为参数 ——
    # 不传的话子进程会用它自己那份默认值：父进程若调过 LLM_MAX_TOKENS，
    # 同一个问题在 MCP 通道与原生通道会生成不同 SQL——这类不一致极难排查。
    "LLM_TEMPERATURE": "llm_temperature",
    "LLM_MAX_TOKENS": "llm_max_tokens",
    "LLM_TIMEOUT": "llm_timeout",
    # —— 数据源（只读连接串）——
    "DB_URL": "db_url",
    "DB_RO_URI": "db_ro_uri",
}


def _child_env(extra: dict | None = None) -> dict[str, str]:
    """构造 stdio 子进程环境：SDK 安全基线 + 白名单（取自 settings）+ 调用方覆盖。

    空值不传：未配置的项留空让子进程自己走默认值，避免把一个空字符串塞进环境
    反而覆盖掉子进程的默认（`AGNES_API_KEY=""` 与「没有这个变量」语义不同）。
    """
    from mcp.client.stdio import get_default_environment

    from src.core.config import settings

    env = dict(get_default_environment())
    for env_name, field in _MCP_ENV_FROM_SETTINGS.items():
        value = getattr(settings, field, None)
        if value is None or str(value) == "":
            continue
        env[env_name] = str(value)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _mcp_to_openai_schema(tool) -> dict:
    """MCP Tool(name/description/inputSchema) → OpenAI function-calling schema。"""
    input_schema = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "").strip() or f"MCP tool: {tool.name}",
            "parameters": input_schema,
        },
    }


class MCPToolBridge:
    """MCP 会话桥：后台线程事件循环 + 常驻会话 + 同步调用接口。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, Any] = {}
        self._stacks: dict[str, Any] = {}     # AsyncExitStack：持住常驻上下文防 GC 断连
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever,
                                 daemon=True, name="mcp-bridge").start()
            return self._loop

    def _submit(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    # ---- 连接（stdio / http） ----
    async def _connect_stdio(self, name: str, command: str, args: list[str],
                             env: dict | None = None) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # 必须显式给 env：SDK 在 server.env 为 None 时只透传安全白名单，
        # 子进程读不到 LLM 凭据（见 _MCP_ENV_FROM_SETTINGS 上方注释）
        params = StdioServerParameters(command=command, args=args,
                                       env=_child_env(env))
        # 常驻连接：AsyncExitStack 持住全部上下文——若不持有 CM 对象引用，
        # GC 回收任务组会直接把连接关掉（实测踩坑：连接秒断 "Connection closed"）
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()
        self._sessions[name] = session
        self._stacks[name] = stack

    async def _connect_http(self, name: str, url: str) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        stack = AsyncExitStack()
        read, write, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(url))
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()
        self._sessions[name] = session
        self._stacks[name] = stack

    def connect_stdio(self, name: str, command: str, args: list[str],
                      env: dict | None = None) -> None:
        self._submit(self._connect_stdio(name, command, args, env), timeout=60)

    def connect_http(self, name: str, url: str) -> None:
        self._submit(self._connect_http(name, url), timeout=60)

    # ---- 发现与调用 ----
    def list_tools(self, name: str) -> list:
        async def _go():
            return (await self._sessions[name].list_tools()).tools
        return self._submit(_go(), timeout=30)

    def call(self, name: str, tool: str, args: dict, timeout: float = 60) -> dict:
        """调远端工具：MCP CallToolResult → 统一 {ok, ...} dict（与原生工具同形）。"""
        async def _go():
            return await self._sessions[name].call_tool(tool, args)

        res = self._submit(_go(), timeout=timeout)
        text = res.content[0].text if res.content else "{}"
        try:
            out = json.loads(text)
        except Exception:
            # 非 JSON 文本响应是合法的 MCP 结果（如纯文本工具）——只有 isError 才算失败
            if getattr(res, "isError", False):
                out = {"ok": False, "error": text[:200]}
            else:
                out = {"ok": True, "text": text}
        if getattr(res, "isError", False):
            out.setdefault("ok", False)
        return out


_BRIDGE: MCPToolBridge | None = None


def get_bridge() -> MCPToolBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = MCPToolBridge()
    return _BRIDGE


# ---------------- 凭据体检闸（启动期，零 LLM 消耗） ----------------
# 判据按**工具集**而不是按 server 名/配置：`execute_readonly_sql` / `compute_metric` /
# `render_chart` 都不调 LLM，只有 `generate_sql` 会建客户端。这样第三方 server
# （天气、日历…）不会因为「我们自己没配 LLM key」而被拦住——那才是真的误拦。
_LLM_DEPENDENT_TOOLS: frozenset[str] = frozenset({"generate_sql"})


def _needs_llm_credentials(spec: dict, tools) -> bool:
    """该 server 是否依赖 LLM 凭据。

    优先用 spec 里显式声明的 `requires_llm`（第三方 server 可能把 LLM 藏在
    一个看不出名字的工具后面）；未声明则按工具集自动判定。
    """
    flag = spec.get("requires_llm")
    if flag is not None:
        return bool(flag)
    return any(getattr(t, "name", "") in _LLM_DEPENDENT_TOOLS for t in tools)


def _gate_llm_credentials(server_name: str, needs: bool, strict: bool) -> bool:
    """凭据闸。返回 True=可以注册工具；False=显式降级跳过（仅 strict=False）。

    strict=True 抛 `MissingCredentialError`：部署错误必须在部署时刻暴露。
    strict=False 打 WARNING 后跳过：**这是「不暴露工具」唯一合法的用法**——
    有意的降级，且留下了声音；而不是悄悄少给模型几个工具（那叫静默失败）。
    """
    if not needs:
        return True
    try:
        require_llm_credentials()
        return True
    except MissingCredentialError as exc:
        first_line = str(exc).splitlines()[0].strip()
        if strict:
            # 保留多行完整修法（缺哪个变量 / .env 还是 setx / 怎么验证）——
            # 这条是抛给**人**看的部署错误，压缩成一行反而丢信息。
            raise MissingCredentialError(
                f"MCP server '{server_name}' 暴露了依赖 LLM 的工具，但{exc}"
            ) from exc
        log.warning(
            "MCP server '%s' 的工具未注册（凭据缺失，strict=False 显式降级）：%s",
            server_name, first_line,      # 日志只留首行：点名到变量即可，修法见 ensure 文档
        )
        return False


def enable_mcp_tools(server_specs: list[dict], *, strict: bool = True) -> list[str]:
    """连接 MCP server → 发现工具 → 注册进 registry。

    server_specs 元素：
      {"name": "别名", "transport": "stdio", "command": "...", "args": [...],
       "env": {...},                        # env 可选，覆盖白名单里的同名项
       "requires_llm": True}                # 可选：显式声明该 server 依赖 LLM 凭据
      {"name": "别名", "transport": "http", "url": "http://host:port/mcp"}

    返回实际注册的工具名列表（与原生四工具同名的会被跳过——原生优先）。

    **凭据体检**：`list_tools` 本身不调 LLM，所以凭据缺失时连接与注册都会成功，失败是
    **惰性**的——要等第一次真正调 `generate_sql` 才炸，还会被上层兜成 degraded。
    于是这里补一道闸，分两个时机：

      · **连接前**：spec 显式写 `"requires_llm": True` 时即可判定。**连本仓库的
        `mcp_server.py` 请务必写上这一项**——它自己带启动期体检，缺凭据会 `sys.exit(1)`，
        客户端只会拿到一条协议层「连接失败」，闸门根本来不及说话。
      · **连接后**：未声明时按**工具集**自动判定（只有 `generate_sql` 建 LLM 客户端），
        这样第三方天气/日历 server 不会因为「我们自己没配 LLM key」被误拦。

    strict=True（默认）：抛 `MissingCredentialError`，把部署错误暴露在**启动时**。
    strict=False：打 WARNING 并**跳过该 server 的工具注册**（模型看不到这些工具）。
        这是唯一的「不暴露工具」合法场景——显式降级，且日志里有声音。
    """
    bridge = get_bridge()
    registered: list[str] = []
    for spec in server_specs:
        name = spec["name"]
        declared = spec.get("requires_llm")     # True / False / None(=按工具集自动判定)
        # ① **连接前**闸：显式声明 requires_llm=True 时才能提前判定（那时拿不到工具集）。
        #    这一步在 server 带启动期体检后变成必需——否则缺凭据时子进程直接 exit(1)，
        #    客户端只会拿到一条协议层「连接失败」，看不出是凭据问题。
        if declared is True and not _gate_llm_credentials(name, True, strict):
            continue
        if spec["transport"] == "stdio":
            bridge.connect_stdio(name, spec["command"], spec.get("args") or [],
                                 spec.get("env"))
        elif spec["transport"] == "http":
            bridge.connect_http(name, spec["url"])
        else:
            raise ValueError(f"未知 MCP transport: {spec['transport']}")
        tools = bridge.list_tools(name)
        # ② **连接后**闸：未显式声明时按工具集自动判定，判定为「不需要」就不拦。
        if declared is None and not _gate_llm_credentials(
                name, _needs_llm_credentials(spec, tools), strict):
            continue          # 显式降级：已打 WARNING，该 server 的工具一个都不注册
        for tool in tools:
            disp = (lambda sn, tn: lambda args: bridge.call(sn, tn, args))(name, tool.name)
            reg = register_external_tool(_mcp_to_openai_schema(tool),
                                         tool.inputSchema or {}, disp)
            if reg:
                registered.append(reg)
    return registered

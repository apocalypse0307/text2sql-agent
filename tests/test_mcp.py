"""
P2-S7 MCP 集成测试：真实协议回环（stdio 子进程拉起 MCP server），不调 LLM。

覆盖：
  ① 协议回环：bridge → stdio 子进程 → initialize → list_tools 发现 ≥4 个工具
  ② 安全不旁路：经 MCP 调 execute_readonly_sql("DROP TABLE orders") 必被拦截；
     白名单外表同样拦——MCP 只是协议外壳，安全语义不变
  ③ 正常查询回环：SELECT COUNT(*) 经 MCP 执行返回结果行
  ④ 动态注册：与原生同名的工具被跳过（原生优先）；新名字工具注册成功且
     dispatch() 可调（Pydantic 校验通道生效：缺必填参数返回校验错误）
  ⑤ schema 适配：MCP inputSchema → OpenAI schema / Pydantic 模型转换正确性
  ⑥ 子进程凭据传递：白名单取自 settings 而非 os.environ（零 LLM、不依赖运行环境）
  ⑦ 凭据缺失的失败形态：启动期 fail-fast（strict）+ 调用期结构化错误 + 不误拦第三方 server

运行：python -m pytest tests/test_mcp.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "mcp_server.py"


@pytest.fixture(scope="module")
def bridge():
    """常驻 stdio 会话。

    ★ 凭据不在时用 `--allow-degraded` 起：server 仍能提供 3 个不需要 LLM 的工具，
    只有 generate_sql 缺席（要它的用例自行 skip）。
    否则 server 的启动期体检会硬失败（exit 1），**整个文件里那些压根不需要 LLM 的用例
    全部 error**——用「安全启动」误伤了不该依赖它的测试。
    """
    from src.core.llm import has_llm_credentials
    from src.mcp_client import MCPToolBridge

    args = [str(SERVER)] + ([] if has_llm_credentials() else ["--allow-degraded"])
    b = MCPToolBridge()
    b.connect_stdio("p2", sys.executable, args)
    yield b


# ---------------- ① 协议回环 ----------------

def test_list_tools_discovers_expected_tools(bridge):
    """工具集随凭据状态二态：齐备 4 个；缺失（降级启动）3 个 —— 但**不许静默少给**。"""
    from src.core.llm import has_llm_credentials

    tools = bridge.list_tools("p2")
    names = {t.name for t in tools}
    assert {"execute_readonly_sql", "compute_metric", "render_chart"} <= names
    if has_llm_credentials():
        assert "generate_sql" in names
    else:
        assert "generate_sql" not in names, "缺凭据时不该暴露用不了的工具"
    for t in tools:
        assert t.inputSchema.get("type") == "object"
        assert t.description  # 每个工具都有描述（模型选工具的依据）


# ---------------- ② 安全不旁路 ----------------

def test_mcp_drop_table_blocked(bridge):
    out = bridge.call("p2", "execute_readonly_sql", {"sql": "DROP TABLE orders"})
    assert out.get("ok") is False
    assert "安全拦截" in out.get("error", "") or "仅允许" in out.get("error", "")


def test_mcp_nonwhitelist_table_blocked(bridge):
    out = bridge.call("p2", "execute_readonly_sql",
                      {"sql": "SELECT * FROM secret_table"})
    assert out.get("ok") is False
    assert "越权" in out.get("error", "") or "白名单" in out.get("error", "")


# ---------------- ③ 正常查询回环 ----------------

def test_mcp_select_roundtrip(bridge):
    out = bridge.call("p2", "execute_readonly_sql",
                      {"sql": "SELECT COUNT(*) AS n FROM orders"})
    assert out.get("ok") is True
    assert out["rows"][0]["n"] > 0


def _judge_generate_sql(out: dict, *, credentials_present: bool) -> tuple[str, str]:
    """把 generate_sql 的结果判成三类：`ok` / `skip:<原因>` / `fail:<原因>`。

    抽成**纯函数**的理由：Task B 的核心规则是「凭据不在 → skip；凭据在却报缺 → 红」，
    而后者在加了 server 启动期体检之后，对自家 server **已不可达**（子进程直接退出，
    根本走不到调用期）。埋在真 LLM 用例里的分支等于永远没人验——所以把判定逻辑提出来，
    用几条纯单测把规则钉死，真 LLM 用例只负责拿回 out 再交给它判。
    """
    err = str(out.get("error", ""))
    missing = out.get("error_code") == "MISSING_CREDENTIALS" or "Missing credentials" in err
    if missing:
        if credentials_present:
            return "fail", ("凭据在 settings 里已就位，但子进程仍报缺凭据——"
                            f"这是白名单/传参层 bug，不是环境问题：{err[:200]}")
        return "skip", "前置条件不成立：当前 provider 的凭据未配置（部署问题，非代码缺陷）"
    if out.get("ok") is False and ("429" in err or "限流" in err):
        return "skip", "LLM 免费额度 429 限流，非代码缺陷"
    if out.get("ok") is True and str(out.get("sql", "")).upper().lstrip().startswith("SELECT"):
        return "ok", ""
    return "fail", f"既不是成功也不是已知非缺陷形态，需要人看：{out}"


def test_judge_generate_sql_distinguishes_two_kinds_of_red():
    """★ Task B 的规则本身：同样是「缺凭据」这条报错，凭据在不在 → 一个红一个 skip。"""
    missing = {"ok": False, "error_code": "MISSING_CREDENTIALS",
               "retryable": False, "error": "缺少 LLM 凭据：provider='deepseek' 需要 DEEPSEEK_API_KEY"}

    assert _judge_generate_sql(missing, credentials_present=False)[0] == "skip"
    assert _judge_generate_sql(missing, credentials_present=True)[0] == "fail"
    # SDK 原话（没有 error_code 的老形态）也要能认出来，否则修完文案前的老包会被判成 skip
    sdk_form = {"ok": False, "error": "Missing credentials. Please pass an `api_key`"}
    assert _judge_generate_sql(sdk_form, credentials_present=True)[0] == "fail"
    assert _judge_generate_sql({"ok": False, "error": "HTTP 429 rate limited"},
                               credentials_present=True)[0] == "skip"
    assert _judge_generate_sql({"ok": True, "sql": "SELECT 1"}, credentials_present=True)[0] == "ok"
    assert _judge_generate_sql({"ok": True, "sql": "not sql"}, credentials_present=True)[0] == "fail"


@pytest.mark.skipif(
    os.environ.get("RUN_SLOW") != "1",
    # 刻意不把通过数写进 reason：数字会随测试增减漂移，写死只会让 skip 文案变成过期信息
    reason="真实 LLM 用例：默认跳过，使「同一份代码在任何机器上结果一致」；"
           "要跑全量设 RUN_SLOW=1（还需配好凭据）")
def test_mcp_generate_sql_returns_sql(bridge):
    """唯一依赖真实 LLM 的用例。**两道门，各管一件事。**

    ① `RUN_SLOW=1`（`skipif`）：管**通过数可复现**。默认跳过 → 任何机器都是 129 passed /
       2 skipped，跟你有没有 key 无关；要跑真 LLM 才显式开。这与 `test_tools.py` 里那条
       真 LLM 用例同一个开关，口径统一。
    ② 凭据检查（函数体里）：管**开了开关之后的下一次失败**。`RUN_SLOW=1` 但没配 key 时，
       给一条点名变量的 skip，而不是让 SDK 抛一个指向 `OPENAI_API_KEY` 的红。

    ★ 凭据在不在 → 分两种处置（本轮重点）：
      · 凭据**不在**（部署没配）→ `skip`，写清缺哪个变量；
      · 凭据**在**却仍报缺 → **必须红**：说明 `_MCP_ENV_FROM_SETTINGS` 白名单或 stdio 传参层
        有 bug（凭据没送进子进程），是**代码缺陷**，不是环境问题。
    判定逻辑抽在 `_judge_generate_sql()` 里并有独立单测——因为加了 server 启动期体检后，
    这条分支对自家 server 已不可达（子进程启动即退），埋在这里等于永远没人验。

    另需处理网关侧排队超时（`_submit` 的 `.result(timeout)` 抛 `TimeoutError`，**不是 dict**）。
    """
    from src.core.llm import has_llm_credentials

    present = has_llm_credentials()
    if not present:
        from src.core.config import settings
        from src.core.llm import _PROVIDER_CREDENTIAL
        pair = _PROVIDER_CREDENTIAL.get((settings.llm_provider or "").strip().lower())
        env_name = pair[1] if pair else f"{str(settings.llm_provider).upper()}_API_KEY"
        pytest.skip(f"前置条件不成立：provider={settings.llm_provider} 缺 {env_name}，"
                    f"本用例需要真实 LLM；配好凭据后会自动开始跑")

    try:
        out = bridge.call("p2", "generate_sql",
                          {"question": "2017年有多少笔订单？"}, timeout=120)
    except TimeoutError:
        pytest.skip("LLM 免费额度排队超时（>120s），非代码缺陷")

    verdict, reason = _judge_generate_sql(out, credentials_present=present)
    if verdict == "skip":
        pytest.skip(reason)
    assert verdict == "ok", reason
    assert out["sql"].upper().lstrip().startswith("SELECT")


# ---------------- ④ 动态注册 ----------------

@pytest.fixture()
def _fresh_registry():
    """注册测试会改全局 registry，用后恢复快照。"""
    from src.tools import registry
    snap_schemas = list(registry.TOOL_SCHEMAS)
    snap_models = dict(registry.PARAM_MODELS)
    snap_disp = dict(registry._EXTERNAL_DISPATCH)
    yield
    registry.TOOL_SCHEMAS[:] = snap_schemas
    registry.PARAM_MODELS.clear()
    registry.PARAM_MODELS.update(snap_models)
    registry._EXTERNAL_DISPATCH.clear()
    registry._EXTERNAL_DISPATCH.update(snap_disp)


def test_register_duplicate_native_skipped(_fresh_registry):
    from src.tools.registry import register_external_tool, TOOL_SCHEMAS
    n0 = len(TOOL_SCHEMAS)
    schema = {"type": "function",
              "function": {"name": "execute_readonly_sql", "description": "dup",
                           "parameters": {"type": "object", "properties": {}}}}
    assert register_external_tool(schema, {"type": "object", "properties": {}},
                                  lambda a: {}) is None
    assert len(TOOL_SCHEMAS) == n0          # 原生优先，不覆盖


def test_register_new_tool_and_dispatch(_fresh_registry):
    """第三方新工具：注册 → Pydantic 校验 → 转发 → 结果回填；缺必填参数被校验拦下。"""
    from src.tools.registry import dispatch, register_external_tool, TOOL_SCHEMAS
    n0 = len(TOOL_SCHEMAS)
    schema = {"type": "function",
              "function": {"name": "mcp_echo", "description": "回显测试",
                           "parameters": {"type": "object",
                                          "properties": {"text": {"type": "string"}},
                                          "required": ["text"]}}}
    got = {}
    def fake_dispatch(args: dict) -> dict:
        got.update(args)
        return {"ok": True, "echo": args["text"]}
    name = register_external_tool(schema, schema["function"]["parameters"], fake_dispatch)
    assert name == "mcp_echo"
    assert len(TOOL_SCHEMAS) == n0 + 1      # agent 下一轮 bind_tools 可见

    out = dispatch("mcp_echo", {"text": "hello"})
    assert out == {"ok": True, "echo": "hello"} and got["text"] == "hello"

    bad = dispatch("mcp_echo", {})           # 缺必填 → Pydantic 校验拦截（不旁路）
    assert bad["ok"] is False and "参数校验失败" in bad["error"]


# ---------------- ⑤ schema 适配 ----------------

def test_json_schema_to_pydantic():
    from src.tools.models import json_schema_to_pydantic
    import pydantic
    M = json_schema_to_pydantic("t", {
        "type": "object",
        "properties": {"q": {"type": "string"}, "k": {"type": "integer"},
                       "r": {"type": "number"}},
        "required": ["q"],
    })
    inst = M(q="x")                       # 可选字段缺省合法
    assert inst.q == "x" and inst.k is None
    with pytest.raises(pydantic.ValidationError):
        M(k=1)                            # 必填缺失报错


def test_mcp_to_openai_schema_shape():
    from src.mcp_client import _mcp_to_openai_schema

    class FakeTool:
        name = "my_tool"
        description = "  do things  "
        inputSchema = {"type": "object", "properties": {"a": {"type": "string"}}}

    s = _mcp_to_openai_schema(FakeTool())
    assert s["type"] == "function"
    assert s["function"]["name"] == "my_tool"
    assert s["function"]["description"] == "do things"   # strip 生效
    assert s["function"]["parameters"]["type"] == "object"


# ---------------- ⑥ 子进程凭据传递（零 LLM 消耗的回归防线）----------------
#
# 背景（真实踩坑）：MCP stdio 子进程的环境不是父进程 os.environ 的复制——SDK 只透传
# get_default_environment() 的白名单（本机实测仅 12 个变量）。切到 DeepSeek 后子进程
# 报 "Missing credentials"，根因在**传参层**而非密钥配置层。
# 之前 AGNES 能跑只是巧合：子进程的 settings 会读 CWD 的 .env，而 .env 正好有
# AGNES_API_KEY 兜底；换成 deepseek 后 .env 里没有对应项，兜底失效才暴露出来。
#
# ★ 本组用例**不依赖运行环境**：全部用 monkeypatch 伪造 settings 字段来驱动，
#   不读真实 os.environ / .env。这样无论谁在哪台机器上跑，结论都一致
#   （旧写法靠 `os.environ.get(...)` 判断后再 skip，属于「红绿取决于机器」的测试）。

def test_child_env_takes_value_from_settings_not_environ(monkeypatch):
    """★ 核心用例：同时给环境变量和 settings 一个**不同的**值，子进程必须拿到 settings 的。

    这是「取值源是 settings 而不是 os.environ」唯一的决定性证据——
    只断言「值等于 settings」还不够（两者恰好相同时它也会通过），
    必须让两个来源冲突，才能证明读的是哪一个。
    """
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-FROM-ENV")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-FROM-SETTINGS")

    assert _child_env()["DEEPSEEK_API_KEY"] == "sk-FROM-SETTINGS"


def test_child_env_passes_agnes_key_even_when_provider_is_deepseek(monkeypatch):
    """两家的 key 都传：换 provider 只需改 LLM_PROVIDER，不必再动传参层。"""
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "agnes_api_key", "ag-from-settings")
    monkeypatch.setattr(settings, "deepseek_api_key", "ds-from-settings")
    env = _child_env()
    assert env["AGNES_API_KEY"] == "ag-from-settings"
    assert env["DEEPSEEK_API_KEY"] == "ds-from-settings"


def test_child_env_omits_empty_settings_value(monkeypatch):
    """settings 里是空串 → 该变量**不出现**，而不是传一个空串过去。

    `AGNES_API_KEY=""` 与「没有这个变量」对子进程语义不同：前者会被当成
    「配置了但为空」，后者才让它走自己的默认/报错路径。
    """
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "agnes_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    env = _child_env()
    assert "AGNES_API_KEY" not in env
    assert "DEEPSEEK_API_KEY" not in env


def test_child_env_non_secret_configurable_values_are_forwarded(monkeypatch):
    """非机密的行为参数也要传：否则父子进程默认值不同 → 同一问题两条通道生成的 SQL 不同。"""
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "llm_max_tokens", 777)
    monkeypatch.setattr(settings, "llm_temperature", 0.5)
    env = _child_env()
    assert env["LLM_MAX_TOKENS"] == "777"      # int → 环境变量必须是 str
    assert env["LLM_TEMPERATURE"] == "0.5"


def test_whitelist_fields_all_exist_on_settings():
    """白名单里的每个字段名必须真的存在于 Settings 上。

    ★ 这条不是形式主义：`_child_env` 用 `getattr(settings, field, None)` 取值，
    **字段名写错会静默退化成「不传」**——于是又回到「子进程缺凭据」那个原始故障，
    而且不报任何错。这条用例就是给静默失败装上报警器。
    """
    from src.core.config import Settings
    from src.mcp_client import _MCP_ENV_FROM_SETTINGS

    fields = set(Settings.model_fields)
    unknown = sorted(f for f in _MCP_ENV_FROM_SETTINGS.values() if f not in fields)
    assert not unknown, f"白名单引用了 Settings 上不存在的字段：{unknown}"


def test_child_env_keeps_safe_baseline_and_no_secret_smuggling(monkeypatch):
    """边界断言：安全基线（PATH）要在，非白名单密钥不进，写库口令不进。

    这是「修好功能」与「不放大泄露面」之间的分界线——
    全量透传也能让功能工作，但那等于把父进程所有密钥交给子进程。
    """
    from src.mcp_client import _MCP_ENV_FROM_SETTINGS, _child_env

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-should-not-leak")
    env = _child_env()
    assert env.get("PATH"), "PATH 必须有，否则子进程起不来"        # SDK 基线仍在
    assert "TAVILY_API_KEY" not in env, "非白名单密钥不得进子进程"
    assert "TAVILY_API_KEY" not in _MCP_ENV_FROM_SETTINGS, "白名单本身也不该收编无关密钥"
    assert "SCENARIO" not in _MCP_ENV_FROM_SETTINGS, "SCENARIO 无 settings 字段且无人消费，属死配置"
    # 写库口令不能进子进程：读路径的子进程没有写权限的必要。
    # 检查的是**值**而不只是键名——DB_URL 里若嵌了 agent_rw 的口令，键名是看不出来的。
    leaked = [k for k, v in env.items() if "agent_rw" in str(v)]
    assert not leaked, f"子进程环境里出现了写库凭据：{leaked}"


def test_child_env_extra_overrides_whitelist(monkeypatch):
    """调用方 spec.env 可覆盖白名单同名项（如只给某个 server 换模型）。"""
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "llm_model", "from-settings")
    assert _child_env({"LLM_MODEL": "x-probe"}).get("LLM_MODEL") == "x-probe"


# ---------------- ⑦ 凭据缺失的失败形态（启动期 fail-fast + 调用期结构化错误）----------------
#
# 背景：list_tools 本身不调 LLM，所以凭据缺失时**连接与注册都会成功**，失败是惰性的——
# 要等第一次真正调 generate_sql 才炸，且被 answer* 的 except Exception 兜成 degraded。
# 用户看到的现象是「Agent 好像不太会写 SQL」，而不是「你少配了一个变量」。
#
# 这组的性质：
#   · 启动期：暴露 LLM 工具的 server，凭据没配好就**在注册前**失败（strict）或喊一声降级（非 strict）
#   · 调用期：SDK 原话（指向 OPENAI_API_KEY）必须被翻译成我们自己的变量名 + retryable=False
#   · 边界：**只依赖 LLM 的 server 才检**——第三方 server 不该因我们没配 key 被误拦
# 全部零 LLM 消耗、不依赖真实环境变量。

def _no_llm_credentials(monkeypatch):
    """把当前 provider 的凭据置空（走 monkeypatch，不动真实环境/.env）。"""
    from src.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "")


def test_get_llm_missing_key_raises_typed_error_naming_real_variable(monkeypatch):
    """★ 决定性断言：环境变量里**有** key、settings 里**没有** → 仍必须失败。

    如果实现偷懒去读 `os.environ`（或让 SDK 自己去看 OPENAI_API_KEY），
    这个用例就会「通过构建」而静默走错源。同时钉住报错文案：
    必须点名 `DEEPSEEK_API_KEY`，且**不得**出现本项目根本不用的 `OPENAI_API_KEY`。
    """
    from src.core.llm import MissingCredentialError, get_llm

    _no_llm_credentials(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-FROM-ENV-ONLY")

    with pytest.raises(MissingCredentialError) as ei:
        get_llm()
    msg = str(ei.value)
    assert "DEEPSEEK_API_KEY" in msg, "报错必须点名到真实变量，否则用户不知道配哪个"
    assert "OPENAI_API_KEY" not in msg, "不许把 SDK 那套无关变量名漏给用户"


def test_require_llm_credentials_rejects_unknown_provider(monkeypatch):
    """provider 拼错不猜默认：静默跑另一家比直接失败更难查。"""
    from src.core.config import settings
    from src.core.llm import MissingCredentialError, require_llm_credentials

    monkeypatch.setattr(settings, "llm_provider", "deapseak")   # 故意拼错
    with pytest.raises(MissingCredentialError) as ei:
        require_llm_credentials()
    assert "deapseak" in str(ei.value)
    assert "deepseek" in str(ei.value), "报错要给出可选值，否则用户只能猜"


def test_generate_sql_returns_structured_error_and_marks_not_retryable(monkeypatch):
    """调用期：缺凭据 → 结构化 dict（ToolMessage 是喂模型的，必须能读懂「别重试」）。"""
    from src.tools.registry import _tool_generate_sql

    _no_llm_credentials(monkeypatch)
    out = _tool_generate_sql({"question": "2017 年各州订单量"})

    assert out["ok"] is False
    assert out["error_code"] == "MISSING_CREDENTIALS"
    assert out["retryable"] is False, "缺配置不是换参数重试能解决的"
    assert "DEEPSEEK_API_KEY" in out["error"]
    assert "\n" not in out["error"], "回喂模型的一行就够，不要塞整段修法手册"


def test_needs_llm_credentials_detects_from_tool_set():
    """判据按**工具集**：只有 generate_sql 建 LLM 客户端，其余三个纯本地。"""
    from types import SimpleNamespace

    from src.mcp_client import _needs_llm_credentials

    local_only = [SimpleNamespace(name=n) for n in
                  ("execute_readonly_sql", "compute_metric", "render_chart")]
    assert _needs_llm_credentials({"name": "third_party"}, local_only) is False
    assert _needs_llm_credentials(
        {"name": "p2"}, local_only + [SimpleNamespace(name="generate_sql")]) is True


def test_needs_llm_credentials_spec_override_wins():
    """显式声明 > 自动判定：第三方把 LLM 藏在一个看不出名字的工具后面时用得着。"""
    from types import SimpleNamespace

    from src.mcp_client import _needs_llm_credentials

    tools = [SimpleNamespace(name="ask_smart_thing")]
    assert _needs_llm_credentials({"name": "x"}, tools) is False
    assert _needs_llm_credentials({"name": "x", "requires_llm": True}, tools) is True
    assert _needs_llm_credentials(
        {"name": "p2", "requires_llm": False},
        [SimpleNamespace(name="generate_sql")]) is False


def test_gate_raises_in_strict_mode_with_server_context(monkeypatch):
    """启动期 fail-fast：报错要带上是哪个 server、缺哪个变量。"""
    from src.core.llm import MissingCredentialError
    from src.mcp_client import _gate_llm_credentials

    _no_llm_credentials(monkeypatch)
    with pytest.raises(MissingCredentialError) as ei:
        _gate_llm_credentials("p2", needs=True, strict=True)
    msg = str(ei.value)
    assert "p2" in msg and "DEEPSEEK_API_KEY" in msg
    # 报错要能让人**一步修好**：给出配置位置（.env）而不只是「缺凭据」
    assert ".env" in msg


def test_gate_degrades_with_warning_when_not_strict(monkeypatch, caplog):
    """非 strict：跳过注册但**必须留声**——无日志的自动降级等于制造谜题。"""
    import logging

    from src.mcp_client import _gate_llm_credentials

    _no_llm_credentials(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="src.mcp_client"):
        keep = _gate_llm_credentials("p2", needs=True, strict=False)

    assert keep is False, "降级时不应注册该 server 的工具"
    assert any("p2" in r.message and r.levelno == logging.WARNING for r in caplog.records), \
        "降级必须有 WARNING，否则就是静默失败"


def test_gate_does_not_block_servers_without_llm_tools(monkeypatch):
    """★ 反误拦：我们没配 LLM key，不该连累不依赖 LLM 的第三方 server。"""
    from src.mcp_client import _gate_llm_credentials

    _no_llm_credentials(monkeypatch)
    assert _gate_llm_credentials("weather", needs=False, strict=True) is True


class _FakeBridge:
    """假桥：不拉子进程、不联网，只回放一份工具清单——用来测**接线**而非协议。"""

    def __init__(self, tool_names):
        self.tool_names = list(tool_names)
        self.connected: list[str] = []

    def connect_stdio(self, name, command, args, env=None):
        self.connected.append(name)

    def connect_http(self, name, url):
        self.connected.append(name)

    def list_tools(self, name):
        from types import SimpleNamespace
        return [SimpleNamespace(name=n, description="", inputSchema={"type": "object",
                                                                    "properties": {}})
                for n in self.tool_names]

    def call(self, name, tool, args, timeout=60):
        return {"ok": True}


def test_enable_mcp_tools_wires_the_gate(monkeypatch, _fresh_registry):
    """★接线用例：闸门必须真的被 enable_mcp_tools 调用。

    只测 `_gate_llm_credentials` 本身是不够的——把主流程里那一行 `if not _gate(...)`
    删掉，所有辅助函数用例照样全绿，而线上又会退回惰性失败。
    **「函数正确」与「函数被调用」是两个性质。**
    """
    from src.core.llm import MissingCredentialError
    from src import mcp_client

    _no_llm_credentials(monkeypatch)
    monkeypatch.setattr(mcp_client, "get_bridge", lambda: _FakeBridge(["generate_sql"]))
    spec = [{"name": "p2", "transport": "stdio", "command": "python", "args": []}]

    with pytest.raises(MissingCredentialError):
        mcp_client.enable_mcp_tools(spec)


def test_preconnect_gate_blocks_connecting_when_declared(monkeypatch, _fresh_registry):
    """显式 `requires_llm: True` → 闸门在**连接前**判定，不去拉一个注定 exit(1) 的子进程。

    这条是「服务端加了启动期体检」之后的必要配套：否则缺凭据时子进程先自杀，
    客户端只能拿到一条协议层「连接失败」，我们的可执行报错永远说不出口。
    """
    from src.core.llm import MissingCredentialError
    from src import mcp_client

    _no_llm_credentials(monkeypatch)
    fake = _FakeBridge(["generate_sql"])
    monkeypatch.setattr(mcp_client, "get_bridge", lambda: fake)
    spec = [{"name": "p2", "transport": "stdio", "command": "python", "args": [],
             "requires_llm": True}]

    with pytest.raises(MissingCredentialError):
        mcp_client.enable_mcp_tools(spec)
    assert fake.connected == [], "声明了 requires_llm 就该在连接前拦下"


def test_preconnect_gate_passes_and_connects_when_credentials_present(monkeypatch, _fresh_registry):
    """反向对照：凭据齐备时，前置闸门不得阻止连接与注册。"""
    from src.core.config import settings
    from src import mcp_client

    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-present")
    fake = _FakeBridge(["generate_sql", "weather_now"])
    monkeypatch.setattr(mcp_client, "get_bridge", lambda: fake)
    spec = [{"name": "p2", "transport": "stdio", "command": "python", "args": [],
             "requires_llm": True}]

    registered = mcp_client.enable_mcp_tools(spec)
    assert fake.connected == ["p2"], "凭据齐备时必须真的连上"
    assert registered == ["weather_now"]


def test_enable_mcp_tools_skips_registration_when_degraded(monkeypatch, caplog, _fresh_registry):
    """非 strict 下：工具一个都不注册（返回空列表），并留下 WARNING。"""
    import logging

    from src import mcp_client

    _no_llm_credentials(monkeypatch)
    monkeypatch.setattr(mcp_client, "get_bridge", lambda: _FakeBridge(["generate_sql"]))
    spec = [{"name": "p2", "transport": "stdio", "command": "python", "args": []}]

    with caplog.at_level(logging.WARNING, logger="src.mcp_client"):
        registered = mcp_client.enable_mcp_tools(spec, strict=False)

    assert registered == [], "降级时不得注册任何工具"
    assert any(r.levelno == logging.WARNING and "p2" in r.message for r in caplog.records)


def test_enable_mcp_tools_registers_normally_when_credentials_present(monkeypatch, _fresh_registry):
    """反向对照：凭据齐备时闸门不该妨碍注册（否则上面两条绿得毫无意义）。"""
    from src import mcp_client

    from src.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-present")
    monkeypatch.setattr(mcp_client, "get_bridge",
                        lambda: _FakeBridge(["generate_sql", "weather_now"]))

    registered = mcp_client.enable_mcp_tools(
        [{"name": "p2", "transport": "stdio", "command": "python", "args": []}])

    # generate_sql 与原生同名 → 原生优先跳过；weather_now 是新名字 → 注册成功
    assert registered == ["weather_now"]


# ---------------- ⑧ 启动期验收：把 mcp_server.py 当**黑盒进程**验 ----------------
#
# 立场：这组断言的是**外部契约**，不是内部实现。
#   · 「抛了 MissingCredentialError」是实现细节——哪天换成 sys.exit(2) 或别的异常，
#     只要退出码非零 + 人话文案不变，客户端体验就没变；
#   · 「进程非零退出 + stderr 里点名变量」才是契约：客户端/部署脚本真正能看到的东西。
#
# 同理 stdout 纪律也在这个层面验：stdio 模式下 stdout 是协议信道，一个字节都不能脏。
# 手工起进程、手写 JSON-RPC 帧（不借 SDK），逐行断言全是合法协议帧——
# 借 SDK 验会被 SDK 自己容错掉，看不到脏字节。

_FRAMES = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]


def _env_without_credentials(provider: str = "deepseek") -> dict:
    """故意让子进程「既没 .env 也没环境变量」里能用的凭据。

    做法是把两家 key 都设为**空串**：pydantic-settings 的优先级是
    进程环境 > `.env` 文件，所以空串会覆盖 `.env` 里的真实值（已实测）。
    比「删掉变量」可靠——删掉后子进程会退回读 `.env`，测的就不是缺失场景了。
    """
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = ""
    env["AGNES_API_KEY"] = ""
    env["LLM_PROVIDER"] = provider
    return env


def _env_with_placeholder_credentials(provider: str = "deepseek") -> dict:
    """只求「体检过得了」的环境：给一个假 key。

    启动期体检判的是**非空**，不是**有效**（有效性要到真调用才验证），所以假 key 足以
    把进程带到「健康启动」态。用它而不是继承真实环境，是为了让这条用例
    **不依赖跑测试的人的 shell**：有真 key 时结论一样，没有真 key 时也不该 skip。
    """
    env = dict(os.environ)
    field = "DEEPSEEK_API_KEY" if provider == "deepseek" else "AGNES_API_KEY"
    env["LLM_PROVIDER"] = provider
    env[field] = "sk-PLACEHOLDER-FOR-STARTUP-ONLY"
    other = "AGNES_API_KEY" if provider == "deepseek" else "DEEPSEEK_API_KEY"
    env[other] = ""
    return env


def _run_server_raw(args=(), env=None, timeout: int = 90) -> subprocess.CompletedProcess:
    """起真子进程、喂完整握手 3 帧、关 stdin 让它自己退出，回收 stdout/stderr/退出码。"""
    payload = b"".join(json.dumps(f, ensure_ascii=False).encode() + b"\n" for f in _FRAMES)
    return subprocess.run([sys.executable, str(SERVER), *args],
                          input=payload, capture_output=True, env=env, timeout=timeout)


def _tool_names(proc: subprocess.CompletedProcess) -> list[str]:
    """从 stdout 里取 tools/list 的响应（id=2）→ 工具名列表。"""
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)                 # 脏字节会在这里直接炸，正是我们要的
        if obj.get("id") == 2:
            return [t["name"] for t in obj["result"]["tools"]]
    raise AssertionError("stdout 里没有 tools/list 的响应")


def test_server_exits_nonzero_and_names_the_variable_when_credentials_missing():
    """★ 契约：缺凭据 → 非零退出 + stderr 点名变量 + 给修法；不该「装作启动成功」。"""
    proc = _run_server_raw(env=_env_without_credentials())

    assert proc.returncode != 0, "缺凭据必须以非零码退出，否则部署脚本会以为起来了"
    # 顺序有意为之：最具体的诊断放前面，红了就能一眼归因。
    # 「stdout 有字节」这个缺陷（诊断误打到协议信道）只有它抓得住，
    # 若排在 stderr 断言之后，就会被「变量名不在 stderr」先遮蔽掉（断言会互相遮蔽）。
    assert proc.stdout.strip() == b"", \
        f"失败路径不许在 stdout 留字节（那是协议信道）：{proc.stdout[:200]!r}"
    err = proc.stderr.decode("utf-8", "replace")
    assert "DEEPSEEK_API_KEY" in err, f"stderr 必须点名到具体变量，实际：{err[:300]}"
    assert ".env" in err, "还要告诉人去哪儿配（这是「能一步修好」的最低要求）"


def test_stdio_stdout_is_protocol_only_when_healthy():
    """★ stdout 洁净性：健康启动时 stdout **逐行**都必须是合法 JSON-RPC 帧。

    这条是 stdio 传输的命门——任何一句 print 混进 stdout，客户端都会解析失败/断连。
    所以诊断一律 stderr（`_startup_credential_check` 里全部 print(file=sys.stderr)）。
    """
    proc = _run_server_raw(env=_env_with_placeholder_credentials())

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:400]
    raw = proc.stdout.decode("utf-8", "replace")
    lines = [l for l in raw.splitlines() if l.strip()]
    assert lines, "stdout 一个帧都没有，说明服务根本没应答"
    for i, line in enumerate(lines):
        obj = json.loads(line)                   # 非 JSON 直接失败，无需额外断言
        assert obj.get("jsonrpc") == "2.0", f"第 {i} 行不是 JSON-RPC 帧：{line[:120]}"
        assert set(obj) <= {"jsonrpc", "id", "result", "error", "method", "params"}, \
            f"第 {i} 行不是协议帧：{line[:120]}"
    # 集合断言而非顺序断言：工具顺序不是契约的一部分（条件注册使 generate_sql 排到了最后）
    assert sorted(_tool_names(proc)) == sorted(["generate_sql", "execute_readonly_sql",
                                                "compute_metric", "render_chart"]), \
        "健康启动应暴露全部 4 个工具"


def test_allow_degraded_starts_with_only_three_tools_and_warns():
    """`--allow-degraded`：进程照常起来，但 **generate_sql 不出现在 tools/list 里**。

    这是「不暴露工具」唯一合法的用法：显式声明降级，且 stderr 留了声音。
    """
    proc = _run_server_raw(args=("--allow-degraded",), env=_env_without_credentials())

    assert proc.returncode == 0, "降级模式必须能正常起来，否则 --allow-degraded 没意义"
    names = _tool_names(proc)
    assert "generate_sql" not in names, "缺凭据时不该把用不了的工具暴露给客户端"
    assert sorted(names) == sorted(["execute_readonly_sql", "compute_metric", "render_chart"]), \
        f"其余 3 个工具必须照常在（它们不需要 LLM），实际：{names}"
    err = proc.stderr.decode("utf-8", "replace")
    assert "WARN" in err and "DEEPSEEK_API_KEY" in err, f"降级必须留声，实际：{err[:300]}"


def test_build_server_is_the_only_construction_path(monkeypatch):
    """★ 换入口也绕不过体检——因为构造 server 只剩 build_server 一条路。

    · 缺凭据 + 不降级 → 抛 `MissingCredentialError`。注意是**抛**、不是 `sys.exit`：
      库代码不该杀进程，退出码是 CLI 边界的事（这样 `build_server()` 才能被测试直接调用）。
    · 缺凭据 + `allow_degraded=True` → 能拿到 server，但工具集里**没有** generate_sql
      （fail-closed：没有凭据就不给用不了的工具）。
    """
    from src import mcp_server
    from src.core.config import settings
    from src.core.llm import MissingCredentialError

    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "")

    with pytest.raises(MissingCredentialError):
        mcp_server.build_server()

    server = mcp_server.build_server(allow_degraded=True, quiet=True)
    assert sorted(server._tool_manager._tools) == sorted(
        ["execute_readonly_sql", "compute_metric", "render_chart"])


def test_import_entry_does_not_die_and_stays_fail_closed():
    """import 型入口（`mcp dev` / `from src.mcp_server import mcp`）的两条要求。

    ① 不能 `sys.exit`——否则 `python -c "import src.mcp_server"` 会被直接杀掉
       （以前体检在 `__main__` 里，import 入口干脆没体检；现在是 fail-closed，语义更强）；
    ② 不能带病暴露——模块级对象在缺凭据时**只有 3 个工具**；
    ③ 仍然不许污染 stdout（import 时也不许有杂音）。
    """
    env = _env_without_credentials()
    code = ("import sys, src.mcp_server as m; "
            "print('tools=', len(m.mcp._tool_manager._tools), file=sys.stderr)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, timeout=90)

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:300]
    assert proc.stdout.strip() == b"", "import 入口也不许污染 stdout"
    assert "tools= 3" in proc.stderr.decode("utf-8", "replace"), "缺凭据时必须是 fail-closed 的 3 工具"

"""
同一 thread_id 连续多轮问答的回归测试（2026-10-26）。

起因：三张图都带 MemorySaver，同一个 thread_id 再 invoke 时，LangGraph 从上一轮存下的
state 接着跑，输入里没出现的键原样保留。前端每个会话一直复用同一个 thread_id
（web/index.html 的 threadId），而服务端原先每轮只传 {"question": ...}，于是：
  · agentic   ：steps 越攒越多，连问到第 3 个问题左右秒回「已达步数上限」
  · react     ：上一问被安全拦截（status=blocked），下一问 SQL 完全正常也照样判拦截；
                上一问残留的 sql_error 被当成本轮报错回喂，retry_count 也不归零
  · supervisor：上一问 degraded 的 status / message 粘到下一问
所有评测脚本都给每道题单开 thread_id（s5-{id} / s8-{graph}-{id} …），这条路径从没被测到。

修法：每轮的图输入统一由 turn_input() 生成（answer*() 与 server._stream 共用），
除 messages 外的本轮字段全部清零；messages 照常跨轮累积，追问能力不受影响。

全离线：假 LLM + 假执行器，不连库、不调模型、不读凭据。
运行：python -m pytest tests/test_multiturn.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.types import Overwrite  # noqa: E402

from src.agent import agentic_graph as ag  # noqa: E402
from src.agent import nodes  # noqa: E402
from src.agent import react_graph as rg  # noqa: E402
from src.agent import supervisor_graph as sg  # noqa: E402


class _Resp:
    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = {}


# ---------------- agentic ----------------

class _AgentLLM:
    """每个问题：两次工具调用 + 一次收尾（generate → execute → 总结 的最常见形状）。"""

    def __init__(self):
        self.q, self.k, self.n = None, 0, 0

    def bind_tools(self, *_a, **_k):
        return self

    def invoke(self, msgs, **_k):
        self.n += 1
        q = msgs[-1].content          # agent 节点总把本轮问题放在最后一条
        if q != self.q:
            self.q, self.k = q, 0
        self.k += 1
        if self.k <= 2:
            return AIMessage(content="", tool_calls=[{
                "name": "execute_readonly_sql", "args": {"sql": f"SELECT {self.k} AS n"},
                "id": f"call_{self.n}"}])
        return AIMessage(content=f"结论：{q}")


def test_agentic_same_thread_three_turns_all_ok(monkeypatch):
    fake = _AgentLLM()
    monkeypatch.setattr(ag, "get_llm", lambda: fake)
    monkeypatch.setattr(ag, "dispatch",
                        lambda name, args, reg=None: {"ok": True, "rows": [{"n": 1}], "row_count": 1})
    graph, _cp = ag.build_agentic_graph()
    for q in ("2017年订单数？", "那2018年呢？", "那2016年呢？"):
        st = ag.answer_agentic(graph, q, thread_id="same-session")
        # 修前第 3 问：steps 攒到 8 → 「已达步数上限」degraded
        assert st["status"] == "ok", st.get("message")
        assert st["steps"] == 3, "steps 应按轮计数，不该跨轮累加"
    # 清零的只是本轮字段：三轮的收尾回答都还在 messages 里（追问靠它）
    finals = [m.content for m in st["messages"]
              if isinstance(m, AIMessage) and not m.tool_calls]
    assert finals == ["结论：2017年订单数？", "结论：那2018年呢？", "结论：那2016年呢？"]


# ---------------- react ----------------

class _ReactLLM:
    """understand 原样回问题；generate 依次吐 sqls 里的 SQL，并记下每次收到的 user 提示。"""

    def __init__(self, sqls):
        self.sqls = list(sqls)
        self.gen_prompts: list[str] = []

    def invoke(self, msgs, **_k):
        sys_p = msgs[0].content
        if "口径规范器" in sys_p:
            return _Resp(json.dumps({"clarified": msgs[-1].content, "notes": ""}, ensure_ascii=False))
        if "把问题转成一条 SQL" in sys_p:
            self.gen_prompts.append(msgs[-1].content)
            return _Resp(self.sqls.pop(0))
        return _Resp("解读：共 1 笔")


def _react_offline(monkeypatch, fake, exec_results):
    """不碰库：Schema/few-shot 给空，执行器按顺序吐 exec_results 里的 (ok, err, rows)。"""
    seq = list(exec_results)
    monkeypatch.setattr(nodes, "get_llm", lambda: fake)
    monkeypatch.setattr(nodes, "_build_context", lambda q: ("", []))
    monkeypatch.setattr(nodes, "execute_with_timeout", lambda db, sql: seq.pop(0))


def test_react_blocked_does_not_stick_to_next_turn(monkeypatch):
    fake = _ReactLLM(["DROP TABLE orders", "SELECT COUNT(*) AS n FROM orders"])
    _react_offline(monkeypatch, fake, [(True, "", [{"n": 1}])])
    graph, _cp = rg.build_agent_graph()
    st1 = rg.answer(graph, "把订单表删了", thread_id="react-session")
    assert st1["status"] == "blocked"
    st2 = rg.answer(graph, "2017年有多少订单？", thread_id="react-session")
    # 修前：status=blocked 留在 checkpoint 里，route_after_validate 直接走拦截分支
    assert st2["status"] == "ok", st2.get("message")
    assert "安全拦截" not in st2["message"]


def test_react_previous_error_not_fed_back_and_retries_reset(monkeypatch):
    # 第 1 问：四次生成全部执行失败 → degraded（retry_count=3、sql_error 残留）
    # 第 2 问：首轮执行失败一次，自愈后成功
    fake = _ReactLLM(["SELECT bad FROM orders"] * 4
                     + ["SELECT x FROM orders", "SELECT n FROM orders"])
    err = (False, "OperationalError: no such column: bad", [])
    _react_offline(monkeypatch, fake, [err] * 4 + [err, (True, "", [{"n": 1}])])
    graph, _cp = rg.build_agent_graph()
    st1 = rg.answer(graph, "第一问", thread_id="react-retry")
    assert st1["status"] == "degraded"
    st2 = rg.answer(graph, "第二问", thread_id="react-retry")
    # 修前：第 2 问的首次生成就带着第 1 问的报错；retry_count=3 还让它一失败就放弃
    assert "执行失败" not in fake.gen_prompts[4], fake.gen_prompts[4]
    assert st2["status"] == "ok", st2.get("message")


# ---------------- supervisor ----------------

class _SupLLM:
    def __init__(self):
        self.sql = "SELECT COUNT(*) AS n FROM orders"

    def invoke(self, msgs, **_k):
        sys_p = msgs[0].content
        if "任务规划器" in sys_p:
            return _Resp(json.dumps({"plan": [{"agent": "sql_agent", "task": "查数"},
                                              {"agent": "analysis_agent", "task": "分析"}]},
                                    ensure_ascii=False))
        if "SQL 工程师" in sys_p:
            return _Resp(json.dumps({"sql": self.sql}))
        if "数据分析师" in sys_p:
            return _Resp("共 45090 笔")
        if "数据质量判官" in sys_p:
            return _Resp(json.dumps({"verdict": "pass", "issues": "none"}))
        return _Resp("?")


def test_supervisor_degraded_does_not_stick_to_next_turn(monkeypatch):
    fake = _SupLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: fake)
    monkeypatch.setattr(sg, "_schema_block", lambda: "(schema)")
    monkeypatch.setattr(sg, "execute_with_timeout", lambda db, sql: (True, "", [{"n": 45090}]))
    graph, _cp = sg.build_supervisor_graph()

    fake.sql = "DROP TABLE orders"            # 第 1 问：两次都被安全层拦 → degraded
    st1 = sg.answer_supervisor(graph, "把订单表删了", thread_id="sup-session")
    assert st1["status"] == "degraded"

    fake.sql = "SELECT COUNT(*) AS n FROM orders"
    st2 = sg.answer_supervisor(graph, "2017年有多少订单？", thread_id="sup-session")
    # 修前：status=degraded 粘住，respond 原样吐出第 1 问的「SQL 被安全层拦截」
    assert st2["status"] == "ok", st2.get("message")
    assert "45090" in st2["message"]


# ---------------- server：SSE 入口也必须用 turn_input ----------------

def test_server_stream_feeds_turn_input(tmp_path, monkeypatch):
    """服务端才是网页的真实路径：只修 answer*() 不修 _stream，浏览器里照样串。"""
    import src.api.server as server

    monkeypatch.setattr(server, "_SVC_DB", tmp_path / "service.db")
    server._svc_init()
    seen: list[dict] = []

    class _FakeGraph:
        def stream(self, inp, config=None, stream_mode=None):
            seen.append(inp)
            # _stream 用多路 stream_mode=["updates","messages"]，每块是 (mode, chunk)
            yield ("updates", {"respond": {"status": "ok", "message": "好的", "result": []}})

    monkeypatch.setattr(server, "_get_graph", lambda graph_type: (_FakeGraph(), None))

    async def _drain(agen):
        return [piece async for piece in agen]

    for graph_type in ("agentic", "react", "supervisor"):
        req = server.QueryRequest(question="2017年订单数？", thread_id="same", graph=graph_type)
        frames = asyncio.run(_drain(server._stream(req)))
        assert any('"type": "done"' in f for f in frames), frames
    assert len(seen) == 3
    # supervisor 的计数字段带 reducer，清零值包在 Overwrite 里（见 supervisor_graph.turn_input）
    def _plain(v):
        return v.value if isinstance(v, Overwrite) else v

    for inp in seen:
        assert _plain(inp.get("steps")) == 0 and inp.get("status") == "ok" \
            and _plain(inp.get("token_cost")) == 0, inp

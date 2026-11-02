"""
工具注册表 dispatch + Pydantic 参数双保险 的 pytest 套件（P2-S4）。

覆盖三类拦截（对应「Function Calling 双保险」）：
  - 缺失必填字段     → dispatch 返回 {ok: False, "参数校验失败: ..."}
  - 类型错误 / 非法枚举 → Literal/类型约束在 Pydantic 层直接拒绝
  - 恶意参数（SQL 含 DROP / 超长 / 非法表）→ 参数模型字段校验器兜底
同时验证：合法参数能一路走到工具实现并返回 ok（generate_sql 走真实 LLM，单独标注慢用例）。

运行：.venv/Scripts/python.exe -m pytest tests/test_tools.py -v
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from src.tools.registry import TOOL_SCHEMAS, dispatch

# ---------------- 元数据：Schema 与模型一一对应 ----------------

def test_tool_schemas_are_four_functions():
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    assert names == ["generate_sql", "execute_readonly_sql", "compute_metric", "render_chart"]
    for s in TOOL_SCHEMAS:
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"


def test_dispatch_unknown_tool():
    r = dispatch("hack_tool", {"a": 1})
    assert r["ok"] is False and "未知工具" in r["error"]


# ---------------- 第二道保险：参数拦截（不烧 LLM） ----------------

MISSING_FIELD_CASES = [
    # 缺必填 question
    ("generate_sql", {}),
    # 缺必填 sql
    ("execute_readonly_sql", {}),
    # 缺 metric（data_ref/value_col 都在但少 metric）
    ("compute_metric", {"data_ref": "result", "value_col": "price"}),
    # 缺 chart_type
    ("render_chart", {"x": "a", "y": "b", "data_ref": "result"}),
]


@pytest.mark.parametrize("name,args", MISSING_FIELD_CASES)
def test_missing_field_rejected(name, args):
    r = dispatch(name, args)
    assert r["ok"] is False
    assert "参数校验失败" in r["error"], r


TYPE_AND_ENUM_CASES = [
    # 类型错：question 不是 str
    ("generate_sql", {"question": 123}),
    # 类型错：sql 是 list
    ("execute_readonly_sql", {"sql": ["SELECT 1"]}),
    # 枚举非法：metric 不在白名单
    ("compute_metric", {"metric": "median", "data_ref": "result", "value_col": "x"}),
    # 枚举非法：chart_type 用折线图英文全名
    ("render_chart", {"chart_type": "linechart", "x": "a", "y": "b", "data_ref": "r"}),
    # 枚举非法：scatter 不在 render_chart 白名单（models 只允许 bar/line/pie）
    ("render_chart", {"chart_type": "scatter", "x": "a", "y": "b", "data_ref": "r"}),
]


@pytest.mark.parametrize("name,args", TYPE_AND_ENUM_CASES)
def test_type_and_enum_rejected(name, args):
    r = dispatch(name, args)
    assert r["ok"] is False
    assert "参数校验失败" in r["error"], r


MALICIOUS_CASES = [
    # SQL 里塞 DROP（Pydantic 字段校验器快速失败，AST 层另有单测兜底）
    ("execute_readonly_sql", {"sql": "SELECT * FROM orders; DROP TABLE orders"}),
    ("execute_readonly_sql", {"sql": "DELETE FROM orders"}),
    ("execute_readonly_sql", {"sql": "UPDATE orders SET order_status='x'"}),
    ("execute_readonly_sql", {"sql": "ATTACH DATABASE 'x' AS y"}),
    # 超长 SQL（>4000 字符）拒绝
    ("execute_readonly_sql", {"sql": "SELECT " + "1," * 4000 + " 1"}),
    # 问题超长
    ("generate_sql", {"question": "q" * 600}),
]


@pytest.mark.parametrize("name,args", MALICIOUS_CASES)
def test_malicious_args_rejected(name, args):
    r = dispatch(name, args)
    assert r["ok"] is False
    # 恶意 SQL：可能被字段校验器拦（禁止关键字）或长度拦——都算拦截成功
    assert "参数校验失败" in r["error"], r


# ---------------- 合法路径（不烧 LLM 的工具） ----------------

def test_legal_execute_passes_and_runs():
    """合法只读 SELECT：dispatch 直通实现层并真实执行（读库，无 LLM）。"""
    r = dispatch("execute_readonly_sql", {"sql": "SELECT COUNT(*) AS n FROM orders"})
    assert r["ok"] is True, r
    assert r["row_count"] == 1
    assert r["rows"][0]["n"] > 0


def test_execute_blocks_via_validator_layer():
    """白名单外表名：即使过了 Pydantic 字段层，validator 的表白名单也会拦。"""
    r = dispatch("execute_readonly_sql", {"sql": "SELECT * FROM users"})
    assert r["ok"] is False
    assert r.get("blocked") is True and "安全拦截" in r["error"]


def test_compute_metric_on_registered_result():
    """compute_metric 消费 results_registry 里的结果集（无 LLM）。"""
    reg = {"result": [{"sales": 10}, {"sales": 20}, {"sales": 30}]}
    r = dispatch("compute_metric", {"metric": "sum", "data_ref": "result", "value_col": "sales"}, reg)
    assert r["ok"] is True and r["value"] == 60


def test_compute_metric_missing_ref():
    r = dispatch("compute_metric", {"metric": "sum", "data_ref": "nope", "value_col": "x"}, {})
    assert r["ok"] is False and "不存在" in r["error"]


def test_render_chart_rejects_unknown_column():
    """修前列名写错也返回 ok：r.get 取到一排空串和 0，前端画出横轴空白、柱子全 0 的图。"""
    reg = {"result": [{"state": "SP", "n": 3}, {"state": "RJ", "n": 2}]}
    ok = dispatch("render_chart", {"chart_type": "bar", "x": "state", "y": "n", "data_ref": "result"}, reg)
    assert ok["ok"] is True and ok["chart_config"]["series"][0]["data"] == [3, 2]
    bad = dispatch("render_chart", {"chart_type": "bar", "x": "city", "y": "n", "data_ref": "result"}, reg)
    assert bad["ok"] is False and "city" in bad["error"] and "state" in bad["error"]


# ---------------- 合法 generate_sql（真实 LLM，标注慢） ----------------

@pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1",
                    reason="真实 LLM 用例：设 RUN_SLOW=1 才跑（避免默认全跑烧 token）")
def test_legal_generate_sql_real_llm():
    """generate_sql 走真实 AGNES：验证 question→SQL 链路（联网慢用例，默认不随 -v 全跑）。"""
    r = dispatch("generate_sql", {"question": "2017年有多少笔已送达订单？"})
    assert r["ok"] is True, r
    assert r["sql"].lower().startswith("select"), r


# ---------------- agentic tools 节点：每个 tool_call 都要有 ToolMessage 回应（2026-10-26） ----------------

def test_duplicate_tool_calls_each_get_a_tool_message(monkeypatch):
    """AGNES 偶发同一轮重复发同参数 tool_call。修前重复的那个直接 continue、不回 ToolMessage，
    而 OpenAI 协议要求 assistant 消息里每个 tool_call_id 都有回应，缺一条下一轮请求就会被拒。
    修后：只执行一次，两个 id 都拿到同一份结果。"""
    from langchain_core.messages import AIMessage

    from src.agent import agentic_graph as ag

    executed: list[str] = []

    def _fake_dispatch(name, args, reg=None):
        executed.append(name)
        return {"ok": True, "rows": [{"n": 1}], "row_count": 1}

    monkeypatch.setattr(ag, "dispatch", _fake_dispatch)
    args = {"sql": "SELECT COUNT(*) AS n FROM orders"}
    last = AIMessage(content="", tool_calls=[
        {"name": "execute_readonly_sql", "args": args, "id": "call_a"},
        {"name": "execute_readonly_sql", "args": dict(args), "id": "call_b"},
    ])
    out = ag.tools({"messages": [last]})["messages"]
    assert executed == ["execute_readonly_sql"]                # 重复的不再执行
    assert [m.tool_call_id for m in out] == ["call_a", "call_b"]
    assert out[0].content == out[1].content                    # 复用同一份结果，不像「失败」

"""
ReAct 状态图组装（P2-S3 核心，LangGraph 手写 add_node/add_conditional_edges，
禁用 create_react_agent，从零实现）。

图结构（ReAct 循环 = 条件边把失败引回生成器）：

  understand → generate_sql → validate_sql
                                    │ 通过
                                    ▼
                                execute_sql ──成功──► respond ──► END
                                    │
                                    └─失败(可重试)─► self_correct ──► generate_sql（回环，带错误信息）
                                    │                 （错误回喂 = ReAct 的 Observe→Reason）
                                    └─失败(超重试/超预算)─► respond(degraded)

  安全拦截：validate_sql 不通过 ──► respond(blocked) ──► END（留证原因）

防死循环：retry_count 硬上限 + token_cost 预算上限（双保险，见 nodes.py 常量）。
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from src.agent import nodes
from src.agent.state import SQLAgentState, turn_input
from src.core.cost import BudgetExceeded


def build_agent_graph():
    """构建并编译 ReAct 图。返回 (graph, checkpointer)。"""
    checkpointer = MemorySaver()
    g = StateGraph(SQLAgentState)

    # ---------- 注册节点 ----------
    g.add_node("understand", nodes.understand)
    g.add_node("generate_sql", nodes.generate_sql)
    g.add_node("validate_sql", nodes.validate_sql)
    g.add_node("execute_sql", nodes.execute_sql)
    g.add_node("self_correct", nodes.self_correct)
    g.add_node("respond", nodes.respond)

    # ---------- 主链 ----------
    g.set_entry_point("understand")
    g.add_edge("understand", "generate_sql")
    g.add_edge("generate_sql", "validate_sql")

    # validate：拦截 → respond(blocked)；通过 → execute
    g.add_conditional_edges(
        "validate_sql",
        nodes.route_after_validate,
        {"respond": "respond", "execute_sql": "execute_sql"},
    )

    # execute：成功 → respond；失败可重试 → self_correct 回环；超限 → respond(degraded)
    g.add_conditional_edges(
        "execute_sql",
        nodes.route_after_execute,
        {"respond": "respond", "self_correct": "self_correct"},
    )

    # 回环：self_correct（计数+1）→ generate_sql（带 sql_error 重新生成）
    g.add_edge("self_correct", "generate_sql")

    # 终态
    g.add_edge("respond", END)

    graph = g.compile(checkpointer=checkpointer)
    return graph, checkpointer


def answer(graph, question: str, thread_id: str | None = None,
           callbacks=None) -> dict:
    """跑一次问答，返回最终完整 state（invoke 聚合所有节点增量）。
    thread_id 用于多轮会话（checkpointer 持久化 messages）；本轮字段由 turn_input 清零。

    callbacks：可选，LangChain 回调列表（如 CostTracker）——挂上即可实测本条的
    LLM 调用次数与 token/成本，**不改任何业务代码**。
    """
    config: dict = {"configurable": {"thread_id": thread_id or "default"}}
    if callbacks:
        config["callbacks"] = callbacks
    try:
        return graph.invoke(turn_input(question), config=config)
    except BudgetExceeded:
        raise          # ★ 预算熔断必须穿透，见下方 except 的注释
    except Exception as e:  # 图内部异常兜底，返回可读错误
        # 「兜底」与「熔断」的经典冲突：若把 BudgetExceeded 也兜成 degraded 返回，
        # 评测脚本就永远收不到超预算信号，会一路把余额烧干（跑到一半断供、
        # 整轮作废正是要避免的事）。故熔断异常单独放行。
        return {
            "question": question,
            "status": "degraded",
            "message": f"执行异常：{type(e).__name__}: {str(e)[:200]}",
        }

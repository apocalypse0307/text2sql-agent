"""
SQL Agent 状态定义（P2-S3）。

与 P1 的 RAGState 同构设计：TypedDict + add_messages 注解字段。
关键字段设计理由：
  - messages 用 add_messages：LangGraph 的 reducer，跨节点累积对话（含每轮工具调用的
    思考/观察，ReAct 循环的可视化轨迹来源）。
  - clarified_question：understand 节点消歧后的口径（与 P1 路由层 standalone 同思路——
    把「用户怎么说」归一成「任务怎么做」）。
  - retry_count：错误回喂自愈的重试计数，配 MAX_RETRY 硬上限防死循环烧 Token。
  - token_cost：累计 token，超阈值停止重试（Token 成本上限）。
  - result / analysis / chart_config：execute → analyze → respond 三节点的产出。
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages


class SQLAgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]     # 对话历史（含思考/工具轨迹）
    question: str                               # 用户原始问题
    clarified_question: str                     # understand 消歧后的口径
    sql: str                                    # 当前生成的 SQL
    sql_error: str | None                       # 最近一次执行错误（自愈输入）
    retry_count: int                            # 自愈重试计数
    result: list[dict]                          # 查询结果 rows
    analysis: str                               # 指标解读
    chart_config: dict | None                   # ECharts 配置（S6 用）
    token_cost: int                             # 累计 token
    steps: int                                  # agentic 图：已完成 agent 轮数（步数上限防死循环）
    # ⚠️ 下面三行的 "= 默认值" 仅是文档性书写：TypedDict 运行时不生效（不会自动补默认），
    # 实际读取必须统一 state.get(key, 默认) —— 节点代码里正是这么写的。
    status: Literal["ok", "blocked", "degraded"] = "ok"
    message: str = ""                           # 给用户的最终回复
    reject_reason: str = ""                     # 安全拦截原因（留证）


def turn_input(question: str) -> dict:
    """一轮新问答的图输入（react / agentic 共用；supervisor 的在 supervisor_graph.turn_input）。

    为什么必须显式清零：图带 checkpointer，同一个 thread_id 再 invoke 时，LangGraph
    从上一轮存下的 state 接着跑，**输入里没出现的键原样保留**。只传 {"question": ...}
    的话，上一轮的计数器与终态会串进这一轮——
      · agentic 的 steps 越攒越多，连问到第 3 个问题左右就秒回「已达步数上限」；
      · react 上一问被拦截（status=blocked），下一问 SQL 完全正常也走 blocked 分支；
      · 上一问残留的 sql_error 会被 generate_sql 当成本轮报错回喂给模型。
    前端每个会话一直复用同一个 thread_id（追问靠它），所以这就是网页上的默认路径。

    跨轮只留 messages（add_messages 追加，「那 2018 年呢」式追问靠它），其余都是本轮字段：
    步数 / token 预算护栏防的是**单轮**失控（死循环、重试风暴），不是给整个会话设总额。
    """
    return {
        "question": question,
        "clarified_question": "",
        "sql": "",
        "sql_error": None,
        "retry_count": 0,
        "result": [],
        "analysis": "",
        "chart_config": None,
        "token_cost": 0,
        "steps": 0,
        "status": "ok",
        "message": "",
        "reject_reason": "",
    }

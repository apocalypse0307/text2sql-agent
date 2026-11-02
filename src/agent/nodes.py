"""
ReAct 图节点实现（P2-S3 核心，从零手写，禁用 create_react_agent）。

节点清单与职责：
  understand    —— 理解指标口径：LLM 结构化输出澄清后的问题；含日期/状态默认口径规则
  generate_sql  —— LLM 生成 SQL（Schema 注入 + Few-shot Top-3 检索 + 错误回喂）
  validate_sql  —— 四层安全校验（validator.validate_sql）；失败 → respond(blocked) 留证
  execute_sql   —— 只读 + 超时执行；错误 → self_correct（回喂修复）或降级
  self_correct  —— 把错误信息写回 messages，retry_count+1（生成器靠消息历史看到错误）
  respond       —— 终态回复：ok（结果+分析）/ blocked（安全拒绝原因）/ degraded（放弃）

ReAct 循环的本质：
  LangGraph 的图本身不循环——循环是「条件边把 execute 失败引回 generate」实现的。
  状态里的 messages 累积「思考→行动→观察」，模型下一轮能看到上一轮的报错，
  这就是错误回喂自愈 = ReAct 的 Act→Observe 步。
  防死循环：retry_count 上限 MAX_RETRY + token_cost 上限 TOKEN_BUDGET，双保险。
"""
from __future__ import annotations

import json
import sqlite3

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.agent.fewshot import FewShotRetriever
from src.agent.prompting import (
    build_system_prompt,
    render_fewshots_block,
    render_schema_block,
)
from src.core.config import settings
from src.core.llm import get_llm
from src.scenarios import get_profile
from src.safety.validator import execute_with_timeout, validate_sql as run_validate

# 防死循环双上限
MAX_RETRY = 3          # 自愈最多重试 3 次（加上首次生成，最多 4 次生成机会）
TOKEN_BUDGET = 40000   # 单轮问答累计 token 上限（AGNES 免费额度保护；每轮由 state.turn_input 清零）

_PROFILE = get_profile()
# 数据库里最大年份（无年份问题时用「最新完整年」兜底口径）
_KNOWN_MAX_YEAR = _PROFILE.known_max_year


def _num_tokens(text: str) -> int:
    """粗略 token 估算：直接按字符数计（usage 缺失时的兜底）。

    有意「宁可高估」：中文 1 字 ≈ 1 token 基本准确，英文按字符数会高估 2-4 倍——
    对 TOKEN_BUDGET 护栏来说高估是安全方向（提前收手好过烧穿额度）。
    真实 token 以响应 usage_metadata 为准，本函数只在网关不回 usage 时兜底。
    """
    return len(text)


def _account_usage(state: dict, resp) -> dict:
    """累计 token（响应 usage 有则用，无则估算），返回 state 增量。"""
    usage = getattr(resp, "usage_metadata", None) or {}
    n = int(usage.get("total_tokens") or 0)
    if not n:
        n = _num_tokens(str(resp.content or ""))
    return {"token_cost": int(state.get("token_cost") or 0) + n}


# ---------------- 上下文构建（跨节点复用） ----------------

def _build_context(question: str) -> tuple[str, list[dict]]:
    """Schema 注入 + Few-shot 检索，一次算好供 generate 用。"""
    db_path = str(settings.db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        from src.db.schema import TABLES
        row_counts = {}
        samples = {}
        for t in TABLES:
            row_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            samples[t] = [tuple(r) for r in con.execute(f"SELECT * FROM {t} LIMIT 2")]
    finally:
        con.close()
    schema_block = render_schema_block(samples=samples, row_counts=row_counts)
    fewshots = FewShotRetriever().retrieve_topk(question, k=3)
    return schema_block, fewshots


# ---------------- 节点 1：understand ----------------

_UNDERSTAND_PROMPT = """你是数据分析口径规范器。把用户的业务问题改写成一句「口径无歧义」的查询需求。

规则：
1. 仅当问题是「最近/最新/今年/N个月」这类**相对时间且无明确年份**时，用 {max_year} 年兜底并显式写明；
   用户给出明确年份（如 2030 年）时一律保留原年份，不得篡改。
2. 金额币种一律 BRL，不换算。
3. 订单状态：默认统计 delivered（已送达），除非用户明确要求其他状态。
4. 涉及「销售额/收入」默认含运费（price + freight_value），除非明确说商品金额。
5. 客户维度去重/复购用 customer_unique_id。
6. 用户点名要查的表若不在数据库 Schema 中（数据库只有 {catalog}），
   不得擅自映射到近似表，应在 clarified 中说明「数据库无此表」。
7. 如果问题本身语义清晰无需改写，原样输出。
只输出 JSON：{{"clarified": "改写后的查询需求", "notes": "口径说明，无则空字符串"}}"""


def understand(state: dict) -> dict:
    question = state["question"]
    llm = get_llm()
    sys_p = _UNDERSTAND_PROMPT.format(max_year=_PROFILE.known_max_year,
                                      catalog=_PROFILE.table_catalog)
    # AGNES 偶发空响应 → 重试一次（P1 踩坑迁移）
    raw = ""
    for _ in (1, 2):
        resp = llm.invoke([SystemMessage(content=sys_p), HumanMessage(content=question)])
        raw = (resp.content or "").strip()
        if raw:
            break
    clarified, notes = question, ""
    try:
        start = raw.find("{")
        data = json.loads(raw[start: raw.rfind("}") + 1]) if start >= 0 else {}
        clarified = data.get("clarified") or question
        notes = data.get("notes") or ""
    except Exception:
        pass
    upd = {"clarified_question": clarified}
    if notes:
        upd["messages"] = [AIMessage(content=f"口径确认：{notes}")]
    upd.update(_account_usage(state, resp))
    return upd


# ---------------- 节点 2：generate_sql ----------------

def generate_sql(state: dict, callbacks=None) -> dict:
    """生成/修复 SQL。

    callbacks：可选。图内调用时**不需要**传——LangChain 回调会沿 run 树自动传播
    （评测挂 CostTracker 即可全程记账）。但自愈专项评测等场景会**直接调用本节点**
    而不经过图，此时没有父 run 上下文，必须显式传入才能记账。
    """
    q = state.get("clarified_question") or state["question"]
    schema_block, fewshots = _build_context(q)
    # 错误回喂：把上一次的执行错误写进 user 消息，模型据此修复（ReAct 的 Observe→Reason）
    err_feedback = ""
    if state.get("sql_error"):
        err_feedback = (
            f"\n\n你上一次生成的 SQL 执行失败：{state['sql_error']}\n"
            f"请分析错误原因并重新生成修正后的 SQL。这是第 {state.get('retry_count', 0)} 次重试。"
        )
    sys_prompt = build_system_prompt(
        schema_block=schema_block,
        fewshots_block=render_fewshots_block(fewshots),
    )
    llm = get_llm()
    raw = ""
    for _ in (1, 2):
        resp = llm.invoke(
            [SystemMessage(content=sys_prompt),
             HumanMessage(content=q + err_feedback)],
            **({"config": {"callbacks": callbacks}} if callbacks else {}),
        )
        raw = (resp.content or "").strip()
        if raw:
            break
    upd = _account_usage(state, resp)
    # 解析 SQL：优先 JSON 包裹，其次代码块/裸 SQL
    sql = ""
    s = raw.strip()
    if s.startswith("{"):
        try:
            sql = (json.loads(s).get("sql") or "").strip()
        except Exception:
            sql = ""
    if not sql:
        import re
        m = re.search(r"```(?:sql)?\s*(.*?)```", raw, re.S | re.I)
        sql = (m.group(1).strip() if m else raw.strip()).rstrip(";")
    upd["sql"] = sql
    upd["sql_error"] = None
    upd["messages"] = [AIMessage(content=f"生成的 SQL：\n{sql}")]
    return upd


# ---------------- 节点 3：validate_sql ----------------

def validate_sql(state: dict) -> dict:
    sql = state.get("sql") or ""
    vr = run_validate(sql)
    if not vr.passed:
        return {
            "status": "blocked",
            "reject_reason": f"[安全拦截·{vr.layer}] {vr.reason}",
            "messages": [AIMessage(content=f"SQL 被安全层拦截：{vr.reason}")],
        }
    # 通过：写入校验后（已注入 LIMIT）的 SQL
    return {"sql": vr.sql, "tables": vr.tables}


# ---------------- 节点 4：execute_sql ----------------

def execute_sql(state: dict) -> dict:
    sql = state.get("sql") or ""
    ok, err, rows = execute_with_timeout(str(settings.db_path), sql)
    if not ok:
        return {
            "sql_error": err,
            "messages": [AIMessage(content=f"执行失败：{err}", name="executor")],
        }
    return {"result": rows, "sql_error": None}


# ---------------- 节点 5：self_correct（计数器，供条件边用） ----------------

def self_correct(state: dict) -> dict:
    return {"retry_count": int(state.get("retry_count") or 0) + 1}


# ---------------- 条件边路由 ----------------

def route_after_validate(state: dict) -> str:
    return "respond" if state.get("status") == "blocked" else "execute_sql"


def route_after_execute(state: dict) -> str:
    if not state.get("sql_error"):
        return "respond"
    # 超预算 or 超重试 → 降级
    if (state.get("token_cost") or 0) > TOKEN_BUDGET:
        return "respond"
    if (state.get("retry_count") or 0) >= MAX_RETRY:
        return "respond"
    return "self_correct"


# ---------------- 节点 6：respond（终态） ----------------

def respond(state: dict) -> dict:
    status = state.get("status", "ok")
    if status == "blocked":
        return {"message": state.get("reject_reason", "请求被安全策略拒绝")}
    if state.get("sql_error"):
        return {
            "status": "degraded",
            "message": (
                f"已尝试 {state.get('retry_count', 0) + 1} 次仍无法正确执行。"
                f"最后一次错误：{state['sql_error']}\n"
                "建议：检查问题口径或联系数据负责人确认表结构。"
            ),
        }
    rows = state.get("result") or []
    q = state.get("clarified_question") or state["question"]
    if not rows:
        return {
            "status": "ok",
            "message": f"查询执行成功，但【{q}】在数据范围内没有匹配记录（空结果）。\n"
                       f"可能原因：①时间范围超出 {_PROFILE.data_range}；②口径过严。请调整条件后重试。",
            "result": [],
        }
    # 生成结果解读（LLM 总结，避免直接把大表格甩给用户）
    sample = rows[:20]
    preview = json.dumps(sample[:5], ensure_ascii=False, default=str)
    sys_p = (
        "你是电商数据分析助手。用户问了一个问题，下面是 SQL 查询结果（最多展示前20行，"
        "预览为前5行）。用中文给出一段简洁的结论解读：先说结论数字，再补充关键发现。\n"
        f"问题：{q}\n结果共 {len(rows)} 行，预览：{preview}"
    )
    llm = get_llm()
    raw = ""
    for _ in (1, 2):
        # ⚠️ AGNES 网关要求消息里必须含 user 角色（只有 system 会报 No user query found）
        resp = llm.invoke([
            SystemMessage(content="你是电商数据分析助手，用中文简洁解读查询结果。"),
            HumanMessage(content=sys_p),
        ])
        raw = (resp.content or "").strip()
        if raw:
            break
    upd = _account_usage(state, resp)
    upd["status"] = "ok"
    upd["analysis"] = raw or "（解读生成失败，请查看上方结果表格）"
    upd["message"] = raw or f"查询成功，共 {len(rows)} 行结果。"
    return upd


# ---------------- 图构建（react_graph.py 会调用） ----------------

def make_nodes():
    """返回所有节点函数与路由函数（图构建统一入口）。"""
    return {
        "understand": understand,
        "generate_sql": generate_sql,
        "validate_sql": validate_sql,
        "execute_sql": execute_sql,
        "self_correct": self_correct,
        "respond": respond,
        "route_after_validate": route_after_validate,
        "route_after_execute": route_after_execute,
    }

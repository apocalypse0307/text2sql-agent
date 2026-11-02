"""
Supervisor 多智能体总图（P2-S8 核心，手写 StateGraph，禁用 prebuilt）。

三种编排对照：
  S3 react_graph      确定性管线：流程代码写死，LLM 只做 generate 一件事
  S4 agentic_graph    LLM 自主工具循环：单 Agent 自主决定调什么工具
  S8 supervisor_graph 任务分解 + 分工 + 质量门控：Planner 拆解 → 三个子 Agent 按依赖执行
                      （analysis / viz 并行）→ Judge 裁决（LLM-as-Judge），不合格打回重做（revision≤2）

黑板模式（Blackboard）——「子 Agent messages 隔离 + 共享黑板」的实现口径：
  - 每个子 Agent 的 LLM 对话历史是**节点内局部变量**，不进全局 State（互相看不见
    对方的碎碎念，上下文干净、token 省）；
  - 子 Agent 只把**产出物**（SQL / 结果集 / 分析文本 / 图表配置）写进共享 blackboard；
  - Judge 只看黑板上的产出物 + 原始问题裁决——不看任何子 Agent 的内部对话。
  这与多智能体系统的两种主流协作模式（消息总线 vs 黑板）中的黑板模式同构。

图结构（全部 add_node / add_conditional_edges 手写）：
                             ┌──► analysis_agent ──┐
    planner ──► sql_agent ───┤                     ├──► judge ──► respond ──► END
                             └──► viz_agent ───────┘      │
                 ▲ verdict=fix, revision≤2：打回指定子 Agent │
                 └──────────────────────────────────────────┘
  analysis 和 viz 都只读黑板上的 result，互不依赖，sql_agent 之后 fan-out 到同一个超步
  里并行跑（viz 仅在 plan 里有它时才分叉），两条分支都只有一拍长，judge 在下一拍只触发一次。
  打回 sql_agent 会重新分叉；打回 analysis / viz 只重跑它自己，直接回 judge。

并行的三条写入纪律（同一超步里多个节点写同一字段，没有 reducer 会 InvalidUpdateError）：
  - blackboard 用按 key 合并的 reducer，每个节点**只写自己那一格**——整块回写会让并行的
    另一方（或上一轮的旧副本）把刚产出的格子盖掉；
  - steps / token_cost 用 operator.add，节点只写**增量**——写「旧值 + n」会被加两遍；
  - reducer 下写 0 / {} 不再是清零，每轮开头的重置必须用 Overwrite 绕过 reducer（turn_input）。

质量门控（Judge 两道）：
  ① Python 硬门控：分析文本非空、结果与结论一致性抽查（有行却称无数据的矛盾检测）
  ② LLM-as-Judge 软门控：数字溯源（结论中的数字能否在结果集中找到）、口径合规
    （默认 delivered / 年份口径 / 去重键）、图表字段与结果列匹配
  verdict=fix 时必须给出 fix_target（sql/analysis/viz 之一）+ 可执行的修改指令。
"""
from __future__ import annotations

import json
import logging
import operator
import re
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Overwrite

from src.agent.nodes import _num_tokens
from src.core.config import settings
from src.core.cost import BudgetExceeded
from src.core.llm import get_llm
from src.scenarios import get_profile
from src.safety.validator import execute_with_timeout, validate_sql
from src.tools.registry import dispatch

MAX_REVISION = 2          # Judge 打回重做上限（防 judge-死循环烧 token）
MAX_NODE_STEPS = 16       # 全图 LLM 调用次数上限（planner+子agent+judge 合计）
_PROFILE = get_profile()
_log = logging.getLogger(__name__)
_KNOWN_MAX_YEAR = _PROFILE.known_max_year    # 与 S3/S4 同源：场景包统一提供

# 三类子 Agent 的职责边界（prompt 里的角色卡，互相隔离）
_ROLE_CARDS = {
    "sql_agent": "SQL 工程师：只负责把数据需求变成可执行的正确 SELECT，并执行拿到结果行。",
    "analysis_agent": "数据分析师：只负责基于已有结果行写业务结论，禁止编造结果里没有的数字。",
    "viz_agent": "可视化工程师：只负责把结果行变成 ECharts 图表配置（bar/line/pie）。",
}


def _merge_board(old: dict | None, new: dict | None) -> dict:
    """黑板 reducer：按 key 合并。独占格后写覆盖前写；共享格 notes 是备注本，
    同一拍 analysis / viz 可能都往里写，按普通格覆盖会丢掉先合并的那条，所以拼接。
    拼接时跳过已有的备注：打回重做的节点会再写一遍同样的话。
    也因为是拼接，节点写 notes 只能写本次新增的（同 steps 只写增量）。"""
    merged = {**(old or {}), **(new or {})}
    if new and "notes" in new:
        kept = list((old or {}).get("notes") or [])
        merged["notes"] = kept + [n for n in new["notes"] or [] if n not in kept]
    return merged


# 独占格只有一个主人：节点一拍只跑一次、格子又不共用，同一拍就不可能两人写同一格。
# _merge_board 碰上独占格多写不报错，后合并的静默胜出，所以这条纪律只能在写入点强制——
# reducer 里查不了，它分不清「同一拍两人写」和「下一拍打回重做的正常覆盖」。
# 共享格谁都能写，同拍多写由 _merge_board 拼接解决。
_BOARD_OWNERS = {
    "sql_agent": frozenset({"sql", "result"}),
    "analysis_agent": frozenset({"analysis"}),
    "viz_agent": frozenset({"chart_config", "chart_spec"}),
    "judge": frozenset({"fix_instruction"}),
}
_SHARED_SLOTS = frozenset({"notes"})


def _board(owner: str, **slots) -> dict:
    """子 Agent / judge 写黑板的唯一入口：只许写 owner 名下的格子和共享格，越界直接抛错。"""
    foreign = slots.keys() - _BOARD_OWNERS[owner] - _SHARED_SLOTS
    if foreign:
        raise ValueError(f"{owner} 越界写黑板：{sorted(foreign)} 不归它管")
    return slots


def _last(_old, new):
    """后写者胜、同一超步多写不报错。只给 emit_note 用：它是瞬态提示，SSE 按节点读
    stream 的 updates，通道里最后剩哪条无所谓，但 analysis / viz 会在同一拍各写一条。"""
    return new


class SupervisorState(TypedDict, total=False):
    question: str
    plan: list[dict]                 # planner 产出 [{agent, task}]
    # 共享黑板 {sql,result,notes,analysis,chart_config,chart_spec(viz 选的 x/y 或 error，给 judge 审图),fix_instruction}
    blackboard: Annotated[dict, _merge_board]
    verdict: str                     # judge 裁决: pass | fix
    fix_target: str                  # judge 指定的重做对象
    judge_issues: str                # judge 给出的问题清单（审计留痕）
    judge_unreviewed: bool           # judge 输出解析不了、未经审查就放行（respond 要标注）
    revision: int                    # 已打回次数
    token_cost: Annotated[int, operator.add]
    steps: Annotated[int, operator.add]    # 全图 LLM 调用计数（防死循环）
    status: Literal["ok", "degraded"]
    message: str
    analysis: str
    result: list[dict]
    chart_config: dict | None
    # 事件提示字段（供 SSE 归一化层转成前端事件；respond 时清空）
    emit_note: Annotated[str, _last]
    emit_sql: str
    emit_result: list | None
    emit_chart: dict | None


# ---------------- 工具函数 ----------------

def _llm_json(llm, sys_prompt: str, user_prompt: str) -> tuple[dict | None, int]:
    """调 LLM 并解析严格 JSON（截取第一个 {...} 块）。

    返回 (解析结果, 本次消耗 token)。token 口径与 S3/S4 一致（usage 优先，缺失按字符估算）
    ——双跑评测要对比 token 成本，Supervisor 每个 LLM 调用都必须记账，漏记就是造假数据。
    AGNES 429 限流：退避 20s 重试一次；空响应：退避 3s 重试一次。
    """
    import time

    tokens = 0
    for attempt in (1, 2):
        try:
            resp = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)])
        except Exception as e:  # noqa: BLE001 —— 429/网络抖动统一退避重试
            if "429" in str(e) or "RateLimit" in type(e).__name__:
                if attempt == 1:
                    time.sleep(20)
                    continue
                raise
            raise
        tokens += _resp_tokens(resp)
        raw = (resp.content or "").strip()
        if raw:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0)), tokens
                except Exception:
                    continue
        if attempt == 1:
            time.sleep(3)
    return None, tokens


def _resp_tokens(resp) -> int:
    """单次响应的 token（usage 优先，缺失按字符估算；口径同 nodes._account_usage）。"""
    usage = getattr(resp, "usage_metadata", None) or {}
    return int(usage.get("total_tokens") or 0) or _num_tokens(str(resp.content or ""))


def _pay(upd: dict, tokens: int) -> dict:
    """把本节点花掉的 token 记进增量（token_cost 的 reducer 是加法，只能写增量）。"""
    if tokens:
        upd["token_cost"] = tokens
    return upd


def _bump(extra: dict | None = None) -> dict:
    """公共状态增量：LLM 调用计数 +1（防死循环护栏）。

    写的是增量 1，不是「旧值 + 1」：并行的两个节点读到同一个旧值，各写旧值+1，
    经 operator.add 一合并就成了 旧值 + 2×(旧值+1)。"""
    upd = {"steps": 1}
    if extra:
        upd.update(extra)
    return upd


def _blackboard(state: dict) -> dict:
    return state.get("blackboard") or {}


def _own_task(state: dict, name: str) -> str:
    """本子 Agent 在 plan 里的任务描述。

    按名字找：被判官点名重做时，不管它在 plan 第几步都拿回自己的任务。
    不在 plan 里（规划漏排、被判官点名补做）时按原问题做。
    """
    for p in state.get("plan") or []:
        if p.get("agent") == name:
            return p.get("task") or state["question"]
    return state["question"]


def _fix_hint(state: dict, target: str) -> str:
    """Judge 打回时给对应子 Agent 的修改指令（没有则空串）。"""
    if state.get("fix_target") != target:
        return ""
    ins = (_blackboard(state).get("fix_instruction") or "").strip()
    return f"\n\n【质量门控打回，必须修正】{ins}" if ins else ""


# ---------------- 节点 1：planner（任务拆解） ----------------

_PLANNER_PROMPT = """你是数据分析任务规划器。把用户的业务问题拆成 2-3 步的执行计划。
子 Agent 只有三种（按此顺序执行，不可调换）：
  sql_agent       —— 查数：产出 SQL 与结果行
  analysis_agent  —— 分析：基于结果行写业务结论
  viz_agent       —— 画图：把结果行变成图表（仅当用户明确要图时加入）

规则：
1. 第一步必须是 sql_agent（没有数据就没有一切）。
2. 用户没要图就不要排 viz_agent。
3. task 字段写清楚该步的口径要求（年份/状态/去重键等）。

只输出 JSON：{"plan": [{"agent": "sql_agent", "task": "..."}, ...]}"""


def planner(state: dict) -> dict:
    llm = get_llm()
    out, tok = _llm_json(llm, _PLANNER_PROMPT,
                         f"用户问题：{state['question']}\n（数据范围约 {_PROFILE.data_range}）")
    plan = (out or {}).get("plan") or []
    # 兜底规整：python 侧强约束（不信任 LLM 的规划）。每个子 Agent 至多出现一次——
    # 路由按名字分发，重复项不会多跑，但 _own_task 只认第一条，留着只会让规划提示误导人
    uniq: list[dict] = []
    for p in plan:
        if p.get("agent") in _ROLE_CARDS and all(q["agent"] != p["agent"] for q in uniq):
            uniq.append(p)
    plan = uniq
    if not plan or plan[0].get("agent") != "sql_agent":
        plan.insert(0, {"agent": "sql_agent",
                        "task": f"查询回答问题所需的数据：{state['question']}"})
    names = " → ".join(p["agent"] for p in plan)
    return _pay(_bump({"plan": plan, "revision": 0,
                       "blackboard": Overwrite({"notes": []}),
                       "emit_note": f"规划完成：{names}"}), tok)


# ---------------- 节点 2：sql_agent（查数 + 一次自愈） ----------------

_SCHEMA_BLOCK_CACHE: str | None = None


def _schema_block() -> str:
    """完整 Schema 注入（DDL + 中文注释 + 采样值），与 S4 generate_sql 工具内部同源。
    首跑双跑评测的教训：sql_agent prompt 只列表名不列字段，模型会猜错列归属
    （b2: 把 customer_zip_code_prefix 当 orders 的列）——能力差距就差在这块上下文。"""
    global _SCHEMA_BLOCK_CACHE
    if _SCHEMA_BLOCK_CACHE is None:
        import sqlite3

        from src.db.schema import TABLES
        from src.agent.prompting import render_schema_block
        con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        try:
            row_counts, samples = {}, {}
            for t in TABLES:
                row_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                samples[t] = [tuple(r) for r in con.execute(f"SELECT * FROM {t} LIMIT 2")]
        finally:
            con.close()
        _SCHEMA_BLOCK_CACHE = render_schema_block(samples=samples, row_counts=row_counts)
    return _SCHEMA_BLOCK_CACHE


_SQL_AGENT_PROMPT = f"""你是{_PROFILE.domain}数据仓库的 SQL 工程师。数据库是 {_PROFILE.dialect} 方言的 {_PROFILE.db_desc}，
数据范围约 {_PROFILE.data_range}。把数据需求写成**单条 SELECT**。

## 库 Schema（字段归属以此为准，严禁臆造列）
{{schema}}

## 库里只有这 9 张表（严禁使用任何其他表名，如 olist_orders_dataset 等 Kaggle 原始文件名）
{_PROFILE.table_catalog.replace('/', ' / ')}
（可用 WITH ... AS 定义 CTE 临时结果集，最终主查询访问的实体表必须出自上表。）

口径规则：
1. 金额一律 BRL，「销售额/收入」默认含运费（price + freight_value）。
2. 订单状态默认 {_PROFILE.default_status}，除非需求明确要求其他状态。
3. 相对时间（最近/今年）用 {_PROFILE.known_max_year} 年兜底；需求给了明确年份就用那年（哪怕查到空）。
4. 客户去重/复购用 {_PROFILE.dedup_key}。
5. 需求中的表若不在上述 9 张内，输出 {{"error": "无此表：<表名>"}}。

只输出 JSON：{{"sql": "SELECT ..."}} 或 {{"error": "..."}}
"""


def sql_agent(state: dict) -> dict:
    llm = get_llm()

    task = _own_task(state, "sql_agent")
    user_prompt = f"数据需求：{task}{_fix_hint(state, 'sql_agent')}"

    sys_prompt = _SQL_AGENT_PROMPT.replace("{schema}", _schema_block())
    plan_out, tok = _llm_json(llm, sys_prompt, user_prompt)
    plan_out = plan_out or {}
    if plan_out.get("error"):
        return _pay(_bump({"blackboard": _board("sql_agent", notes=[f"sql_agent: {plan_out['error']}"]),
                           "emit_note": f"SQL 工程师：{plan_out['error']}"}), tok)

    sql = str(plan_out.get("sql") or "").strip().rstrip(";")
    vr = validate_sql(sql)
    if not vr.passed:
        # 安全闸拦截 → 打回自愈一次（错误回喂），再拦就如实降级
        retry, tok2 = _llm_json(llm, sys_prompt,
                                f"{user_prompt}\n\n【上一版被安全层拦截：{vr.reason}，"
                                f"只能使用这 9 张表：{_PROFILE.table_catalog}，请修正】")
        tok += tok2
        sql2 = str((retry or {}).get("sql") or "").strip().rstrip(";")
        vr2 = validate_sql(sql2)
        if not vr2.passed:
            return _pay(_bump({"status": "degraded",
                               "message": f"SQL 被安全层拦截：{vr.reason}"}), tok)
        sql, vr = sql2, vr2

    ok, err, rows = execute_with_timeout(str(settings.db_path), vr.sql)
    if not ok:
        # 执行错误自愈一次（与 S3/S4 的自愈同思路：错误回喂 → 重生成）
        retry, tok2 = _llm_json(llm, sys_prompt,
                                f"{user_prompt}\n\n【上一版 SQL 执行失败：{err}，请修正】")
        tok += tok2
        sql3 = str((retry or {}).get("sql") or "").strip().rstrip(";")
        vr3 = validate_sql(sql3)
        if not vr3.passed:
            return _pay(_bump({"status": "degraded",
                               "message": f"SQL 执行失败且修正未过校验：{err}"}), tok)
        ok, err, rows = execute_with_timeout(str(settings.db_path), vr3.sql)
        if not ok:
            return _pay(_bump({"status": "degraded",
                               "message": f"SQL 执行失败：{err}"}), tok)
        sql = sql3

    return _pay(_bump({"blackboard": _board("sql_agent", sql=sql, result=rows), "emit_sql": sql,
                       "emit_result": rows[:30],
                       "emit_note": f"SQL 工程师：查得 {len(rows)} 行"}), tok)


# ---------------- 节点 3：analysis_agent（基于结果行写结论） ----------------

def analysis_agent(state: dict) -> dict:
    llm = get_llm()
    bb = _blackboard(state)
    rows = bb.get("result") or []
    task = _own_task(state, "analysis_agent")
    rows_brief = json.dumps(rows[:20], ensure_ascii=False, default=str)

    sys_prompt = f"""{_ROLE_CARDS['analysis_agent']}
数据范围约 {_PROFILE.data_range}。铁律：
1. 结论里的每个数字必须能在结果行里找到（或明确标注为行数合计）；禁止心算出结果集中不存在的数。
2. 结果为空就必须写明「没有匹配的记录」，并解释可能的口径/时间范围原因（如 2030 年超出数据范围、
   点名的表不存在等）。
3. 若黑板备注中有上游说明（如「无此表」），必须如实转述，不得编造该表的数据。
4. 用中文，先给核心数字结论，再给 1-3 条业务发现。"""
    notes = "；".join(bb.get("notes") or [])
    user_prompt = (f"分析需求：{task}\n结果行（JSON）：{rows_brief}"
                   + (f"\n上游备注：{notes}" if notes else "")
                   + _fix_hint(state, "analysis_agent"))

    resp = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)])
    text = (resp.content or "").strip()
    # 只看了前 20 行就要留个备注：判官手里也只有前 20 行，不知道还有没看到的行，
    # 就查不出「把这 20 行加起来当全年合计」这种错
    extra = ([f"analysis_agent: 结果共 {len(rows)} 行，结论只依据前 20 行"]
             if len(rows) > 20 else [])
    # 只写自己那一格（外加共享的 notes）：和 viz_agent 同一拍并行，整块回写会拿手里的
    # 旧 chart_config 盖掉它刚画的图
    return _pay(_bump({"blackboard": _board("analysis_agent", analysis=text, notes=extra),
                       "emit_note": "分析师：结论已产出"}), _resp_tokens(resp))


# ---------------- 节点 4：viz_agent（结果行 → 图表配置） ----------------

def viz_agent(state: dict) -> dict:
    llm = get_llm()
    bb = _blackboard(state)
    rows = bb.get("result") or []
    if not rows:
        # 跳过和失败都把 chart_config 置空：打回 sql 重查后结果变空，上一轮的图不能留在黑板上
        return _bump({"blackboard": _board("viz_agent", chart_config=None, chart_spec=None),
                      "emit_note": "可视化：无数据行，跳过图表"})

    sys_prompt = f"""{_ROLE_CARDS['viz_agent']}
根据结果行选择最合适的图表：类别对比用 bar，趋势用 line，占比用 pie。
只输出 JSON：{{"chart_type": "bar|line|pie", "x": "类别列名", "y": "数值列名"}}"""
    user_prompt = (f"结果行（JSON）：{json.dumps(rows[:20], ensure_ascii=False, default=str)}"
                   f"{_fix_hint(state, 'viz_agent')}")
    out, tok = _llm_json(llm, sys_prompt, user_prompt)
    out = out or {}
    spec = {"chart_type": out.get("chart_type", "bar"),
            "x": out.get("x", ""), "y": out.get("y", "")}
    cfg = dispatch("render_chart", {**spec, "data_ref": "result"}, {"result": rows})
    if cfg.get("ok"):
        return _pay(_bump({"blackboard": _board("viz_agent", chart_config=cfg["chart_config"],
                                                chart_spec=spec),
                           "emit_chart": cfg["chart_config"],
                           "emit_note": "可视化：图表已生成"}), tok)
    err = cfg.get("error", "?")
    return _pay(_bump({"blackboard": _board("viz_agent", chart_config=None,
                                            chart_spec={"error": err},
                                            notes=[f"viz_agent: 图表没画成：{err}"]),
                       "emit_note": f"可视化失败：{err}（不阻塞结论）"}), tok)


# ---------------- 节点 5：judge（LLM-as-Judge 质量门控） ----------------

_JUDGE_PROMPT = f"""你是数据质量判官。审查一次数据分析的产出是否合格，只依据给定材料，不臆测。

审查清单：
1. 数字溯源：分析结论里的每个关键数字，能否在结果行 JSON 中找到对应值？（找不到 = fix）
2. 口径合规：默认 {_PROFILE.default_status} 状态、年份是否正确使用（{_PROFILE.data_range} 之外要有说明）、
   客户去重是否用 {_PROFILE.dedup_key}。
3. 结论与数据一致：结果为空时结论是否如实说明？有数据时是否给出了结论？
4. 图表（chart 字段，null = 没画图）：x/y 列名须在结果行的列中；y 画的指标须与结论讨论的指标一致（结论讲订单数、图画销售额 = fix viz_agent）；有 error = 画图失败，用户要了图就 fix viz_agent，把 error 里的可用列写进修改指令。
5. 备注（notes 字段）：子 Agent 留下的限制说明。备注说结论只依据前 N 行时，结论不得把这些行的合计说成全量合计（违反 = fix analysis_agent）。

裁决规则：
- 轻微瑕疵（如措辞、多一句废话）→ pass（别吹毛求疵）。
- 数字编造 / 口径错误 / 结论与数据矛盾 → fix，并给出 fix_target（sql_agent=数据查错了 /
  analysis_agent=结论写错了 / viz_agent=图错了）与一句可执行的修改指令。

只输出 JSON：{{"verdict": "pass|fix", "fix_target": "analysis_agent",
"issues": "问题清单（pass 时写 none）", "instruction": "给重做者的修改指令"}}"""


def judge(state: dict) -> dict:
    bb = _blackboard(state)
    llm = get_llm()

    # ① Python 硬门控：产出完整性（比 LLM 便宜且稳定）
    if not bb.get("sql"):
        return _bump({"verdict": "fix", "fix_target": "sql_agent",
                      "judge_issues": "黑板上没有 SQL 产出",
                      "revision": int(state.get("revision") or 0) + 1,
                      "emit_note": "判官：缺 SQL 产出，打回 sql_agent"})
    if not (bb.get("analysis") or "").strip():
        return _bump({"verdict": "fix", "fix_target": "analysis_agent",
                      "judge_issues": "黑板上没有分析结论",
                      "revision": int(state.get("revision") or 0) + 1,
                      "emit_note": "判官：缺分析结论，打回 analysis_agent"})

    # ② LLM-as-Judge 软门控
    material = json.dumps({
        "question": state["question"],
        "sql": bb.get("sql"),
        "rows": (bb.get("result") or [])[:20],
        "analysis": bb.get("analysis"),
        "chart": bb.get("chart_spec"),   # 只给 bool 的话 judge 不知道图画了哪两列，清单第 4 条查不了
        "notes": bb.get("notes") or [],
    }, ensure_ascii=False, default=str)
    out, tok = _llm_json(llm, _JUDGE_PROMPT, material)
    out = out or {}
    verdict = str(out.get("verdict") or "").strip().lower()
    # 判官说不清话（重试后仍解析不出 JSON，或 verdict 不是 pass/fix）：放行，但不能悄悄放行。
    # 默认打回也没用：没有打回对象和修改指令，子 Agent 只能原样重做，白烧两轮
    unreviewed = verdict not in ("pass", "fix")
    if unreviewed:
        verdict = "pass"
        _log.warning("judge 输出无法解析，未经审查放行：%r", out)
    revision = int(state.get("revision") or 0)
    if unreviewed:
        issues, note = "判官输出无法解析，本次未经审查", "判官未能完成审查，已放行"
    else:
        issues = str(out.get("issues", ""))[:300]
        note = f"判官裁决：{verdict}" + (f"（{issues[:80]}）" if verdict == "fix" else "")
    upd = _pay(_bump({"verdict": verdict, "judge_unreviewed": unreviewed,
                      "judge_issues": issues, "emit_note": note}), tok)
    if verdict == "fix":
        upd["fix_target"] = out.get("fix_target") if out.get("fix_target") in _ROLE_CARDS \
            else "analysis_agent"
        upd["revision"] = revision + 1
        upd["blackboard"] = _board("judge", fix_instruction=str(out.get("instruction", ""))[:400])
    return upd


# ---------------- 条件边 ----------------

def route_after_sql(state: dict) -> list[str]:
    """sql_agent 之后 fan-out：plan 里排了的 analysis / viz 同一超步并行跑，都没排就直接 judge。

    按依赖分发，不按 plan 下标推进：两个节点同时完成时「当前第几步」没有定义。
    """
    planned = {p.get("agent") for p in state.get("plan") or []}
    nxt = [a for a in ("analysis_agent", "viz_agent") if a in planned]
    return nxt or ["judge"]


def route_after_judge(state: dict) -> str:
    """judge 裁决路由：pass → respond；fix 且未超上限 → 打回指定子 Agent。
    打回 sql_agent 会经 route_after_sql 重新分叉；打回 analysis / viz 只重跑它自己再回 judge
    （串行版打回 analysis 还会顺着 plan 把没毛病的 viz 也重画一遍）。"""
    if state.get("verdict") == "fix" and int(state.get("revision") or 0) <= MAX_REVISION \
            and int(state.get("steps") or 0) < MAX_NODE_STEPS:
        return state.get("fix_target") or "respond"
    return "respond"


# ---------------- 节点 6：respond（终态收口） ----------------

def respond(state: dict) -> dict:
    bb = _blackboard(state)
    analysis = bb.get("analysis") or ""
    rows = bb.get("result") or []
    status = state.get("status") or "ok"
    message = analysis
    if status == "degraded":
         message = state.get("message") or message
    elif state.get("verdict") == "fix":
        message = (f"（质量门控提示：{state.get('judge_issues', '')}，已达打回上限，以下为当前最优结果）\n\n"
                   + analysis)
    elif state.get("judge_unreviewed"):
        message = "（本次结果未经质量审查：判官输出无法解析）\n\n" + analysis
    return {"status": status, "message": message, "analysis": analysis,
            "result": rows, "chart_config": bb.get("chart_config"),
            "emit_note": "", "emit_sql": "", "emit_result": None, "emit_chart": None}


# ---------------- 图构建 ----------------

def build_supervisor_graph():
    """构建并编译 Supervisor 多智能体图。返回 (graph, checkpointer)。"""
    checkpointer = MemorySaver()
    g = StateGraph(SupervisorState)
    g.add_node("planner", planner)
    g.add_node("sql_agent", sql_agent)
    g.add_node("analysis_agent", analysis_agent)
    g.add_node("viz_agent", viz_agent)
    g.add_node("judge", judge)
    g.add_node("respond", respond)

    g.set_entry_point("planner")
    # plan[0] 恒为 sql_agent（python 侧已强约束），没有数据就没有一切
    g.add_edge("planner", "sql_agent")
    # sql_agent → fan-out 到 analysis / viz（路由函数返回列表 = 下一超步同时触发）
    g.add_conditional_edges("sql_agent", route_after_sql,
                            ["analysis_agent", "viz_agent", "judge"])
    # fan-in：两条分支都只有一拍长，同一超步结束，judge 在下一拍只触发一次。
    # 不能用等待边 add_edge([...], "judge")：viz 是可选的，没排它的问题会永远等不到 judge
    g.add_edge("analysis_agent", "judge")
    g.add_edge("viz_agent", "judge")
    # judge：pass → respond；fix → 打回指定子 Agent
    g.add_conditional_edges("judge", route_after_judge,
                            {"sql_agent": "sql_agent", "analysis_agent": "analysis_agent",
                             "viz_agent": "viz_agent", "respond": "respond"})
    g.add_edge("respond", END)
    graph = g.compile(checkpointer=checkpointer)
    return graph, checkpointer


def turn_input(question: str) -> dict:
    """一轮新问答的图输入：除 question 外全部回到初值（道理同 state.turn_input）。

    同一 thread_id 再 invoke 时，没出现在输入里的键原样保留：上一问 degraded 的
    status 和 message 会粘到下一问（respond 看到 status=degraded 就原样吐上一问的话），
    steps 越攒越多还会让 route_after_judge 悄悄关掉判官打回。
    plan / blackboard 这些虽然 planner 每轮都会重写，也一并清掉，不留任何读到残留的机会。

    带 reducer 的字段（blackboard / token_cost / steps）必须用 Overwrite：写 {} 只是
    「合并一个空字典」、写 0 只是「加上 0」，上一轮的值会原样留着。
    """
    return {
        "question": question,
        "plan": [],
        "blackboard": Overwrite({}),
        "verdict": "",
        "fix_target": "",
        "judge_issues": "",
        "judge_unreviewed": False,
        "revision": 0,
        "token_cost": Overwrite(0),
        "steps": Overwrite(0),
        "status": "ok",
        "message": "",
        "analysis": "",
        "result": [],
        "chart_config": None,
        "emit_note": "",
        "emit_sql": "",
        "emit_result": None,
        "emit_chart": None,
    }


def answer_supervisor(graph, question: str, thread_id: str | None = None,
                      callbacks=None) -> dict:
    """跑一次 Supervisor 问答，返回最终完整 state。thread_id 用于隔离多轮。

    callbacks：可选 LangChain 回调（如 CostTracker）。Supervisor 多智能体是否
    「以更少 token 换更稳的质量」，这个账必须实测，不能估算。
    """
    config: dict = {"configurable": {"thread_id": thread_id or "supervisor-default"}}
    if callbacks:
        config["callbacks"] = callbacks
    try:
        return graph.invoke(turn_input(question), config=config)
    except BudgetExceeded:
        raise          # 预算熔断放行，不被下面的兜底吞成 degraded
    except Exception as e:
        return {
            "question": question,
            "status": "degraded",
            "message": f"执行异常：{type(e).__name__}: {str(e)[:200]}",
        }

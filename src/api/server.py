"""
P2-S6 服务化：FastAPI + SSE 流式问答服务。

接口全景：
  POST /api/query       问一句业务问题 → SSE 流（thought/sql/result/chart/answer/error/proposal/done）
  GET  /api/schema      返回 9 表 Schema（表注释 + 字段中文注释，供左栏渲染）
  GET  /api/history/{id}   查询历史会话（含事件轨迹）
  POST /api/feedback    对一次问答打标（好评/差评 + 备注，沉淀评测样本）
  POST /api/confirm     S9 写提案人审（confirm=写凭据执行 / reject=留痕拒绝）
  POST /api/upload_csv  S9 CSV 导入（系统路径建表 + 动态注册）
  GET  /api/health      存活探针

事件协议（data: {json}\n\n 每行一个事件）：
  thought  —— ReAct 思考过程（understand 口径 / 工具调用意图 / 校验结果 / 判官裁决）
  sql      —— 生成的 SQL（前端可展开 + 复制）
  result   —— 结果集（前 30 行预览 + 总数，供右栏表格/图表）
  chart    —— ECharts 配置（render_chart 工具产出，前端直接 setOption）
  answer   —— 最终结论（LLM 解读文本）
  proposal —— S9 写提案审批卡（SQL/影响行数/取证样本/风险说明）
  error    —— 失败/安全拦截

设计要点：
  - 三图可选（?graph=agentic|react|supervisor，默认 agentic 自主工具循环）——与 S3/S4/S8
    三编排对照口径一致；同一 SSE 归一化层（_norm_*）把不同图的节点更新映射成统一事件。
  - thread_id 隔离多轮会话（checkpointer MemorySaver 按 thread 存 messages，
    实现「那 2018 年呢」式追问）。
  - 历史落盘 data/db/service.db（sqlite，纯本地，不入 git）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.core.config import PROJECT_ROOT, settings
from src.scenarios import get_profile

_PROFILE = get_profile()

app = FastAPI(title="Text2SQL Agent API", version="0.6.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 本地演示前端；生产收紧
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 历史存储（data/db/service.db，纯本地 sqlite）
# ---------------------------------------------------------------------------
_SVC_DB = Path(settings.db_path).parent / "service.db"


def _svc_conn() -> sqlite3.Connection:
    # 必须先建父目录：sqlite3.connect() **不会**自己创建缺失的目录，只会抛
    # `sqlite3.OperationalError: unable to open database file`。
    # 这个坑在 Docker 上真实炸过一次：镜像里不带 data/（数据走卷挂载），
    # 容器首次启动时 /app/data/db 不存在 → `import src.api.server` 直接失败，
    # 服务根本起不来。而本地永远复现不了（本地 data/db/ 早就有 135MB 的库）。
    #
    # 放在这里而不是只在 Dockerfile 里 mkdir：**服务应该能自己创建自己要写的数据目录**，
    # 否则任何「干净机器首次部署」都得靠外部先准备好目录结构——那是部署脚本的责任泄漏。
    _SVC_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_SVC_DB)
    con.row_factory = sqlite3.Row
    return con


def _svc_init() -> None:
    with _svc_conn() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS history(
                 id TEXT PRIMARY KEY, question TEXT, events TEXT,
                 answer TEXT, status TEXT, graph TEXT, created_at TEXT)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS feedback(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 history_id TEXT, rating INTEGER, comment TEXT,
                 created_at TEXT)"""
        )
        # P2-S9 写提案表：HITL 的「提案→决策」全留痕
        con.execute(
            """CREATE TABLE IF NOT EXISTS proposals(
                 id TEXT PRIMARY KEY, question TEXT, sql TEXT, stmt_type TEXT,
                 tables TEXT, affected INTEGER, status TEXT,
                 created_at TEXT, decided_at TEXT)"""
        )


_svc_init()

# 恢复历史 CSV 导入表（表本体在 SQLite 里持久；注册信息进白名单/Schema 注入）
from src.db.schema import load_custom_tables
_N_CUSTOM = load_custom_tables()
if _N_CUSTOM:
    print(f"[schema] restored {_N_CUSTOM} custom table(s) from csv import")


def _save_history(rid: str, question: str, events: list[dict],
                  answer: str, status: str, graph: str) -> None:
    with _svc_conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?)",
            (rid, question, json.dumps(events, ensure_ascii=False),
             answer, status, graph,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def _load_history(rid: str) -> dict | None:
    with _svc_conn() as con:
        row = con.execute("SELECT * FROM history WHERE id=?", (rid,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "question": row["question"],
        "events": json.loads(row["events"]), "answer": row["answer"],
        "status": row["status"], "graph": row["graph"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500, description="业务问题")
    thread_id: str | None = Field(None, max_length=64, description="会话ID（追问复用）")
    graph: str = Field("agentic", description="agentic | react")


class FeedbackRequest(BaseModel):
    history_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field("", max_length=500)


class ConfirmRequest(BaseModel):
    proposal_id: str = Field(..., max_length=64)
    action: str = Field("confirm", description="confirm | reject")


# ---------------------------------------------------------------------------
# 图缓存（懒构建：首次请求才 import 图模块，服务启动不烧 token）
# ---------------------------------------------------------------------------
_GRAPHS: dict[str, Any] = {}


def _get_graph(graph_type: str):
    if graph_type not in _GRAPHS:
        if graph_type == "react":
            from src.agent.react_graph import build_agent_graph
            _GRAPHS["react"] = build_agent_graph()
        elif graph_type == "supervisor":
            from src.agent.supervisor_graph import build_supervisor_graph
            _GRAPHS["supervisor"] = build_supervisor_graph()
        else:
            from src.agent.agentic_graph import build_agentic_graph
            _GRAPHS["agentic"] = build_agentic_graph()
    return _GRAPHS[graph_type]


def _turn_input(graph_type: str, question: str) -> dict:
    """本轮的图输入：与 answer*() 用同一份定义，清零上一轮残留的计数器与终态。

    前端每个会话一直带同一个 thread_id（追问要靠它累积 messages），这里如果只传
    {"question": ...}，上一轮的 steps / status 会原样留在 checkpoint 里串进这一轮
    ——agentic 连问到第 3 个问题左右就秒回「已达步数上限」。
    懒导入与 _get_graph 同理：启动时不加载图模块。
    """
    if graph_type == "supervisor":
        from src.agent.supervisor_graph import turn_input
    else:
        from src.agent.state import turn_input
    return turn_input(question)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# 防御：模型偶尔在 answer 文本里塞 <echarts-config>{...}</echarts-config>（图表已由
# render_chart 工具独立产出），同时兼容被前端 HTML 转义后的 &lt;…&gt; 形式
_CHART_XML_RE = re.compile(
    r"<\s*echarts(?:-config)?[^>]*>.*?</\s*echarts(?:-config)?\s*>", re.S | re.I)
_CHART_XML_ESC_RE = re.compile(
    r"&lt;\s*echarts(?:-config)?[^&]*&gt;.*?&lt;/\s*echarts(?:-config)?\s*&gt;", re.S | re.I)


def _strip_chart_xml(s: str | None) -> str:
    if not s:
        return s or ""
    s = _CHART_XML_RE.sub("", s)
    s = _CHART_XML_ESC_RE.sub("", s)
    return s.strip()


def _ev(type_: str, **data: Any) -> dict:
    return {"type": type_, "ts": _utc(), **data}


# ---------------------------------------------------------------------------
# P2-S9 写路径（HITL 旁路）：写意图 → 提案 SQL → 事务 dry-run → 人审 → 执行
# 设计立场：写永不进 LLM 自主循环——模型有建议权，人有否决权与执行权。
# ---------------------------------------------------------------------------
_WRITE_HINT_RE = re.compile(
    r"(插入|新增|添加|写入|录入|更新|修改|改成|改为|删除|删掉|清空|抹掉|去掉)"
)
# 注意 `{{schema}}` 的双花括号不是笔误：这是 f-string，`{{` 渲染成字面量 `{`，
# 于是模板里留下的是 `{schema}` 占位符，由 build_write_proposal() 用
# `.replace("{schema}", schema_block)` 在**运行时**填（那份 schema 依赖库的实际表）。
# 写成单花括号 `{schema}` 会在**导入期**就被求值 → `NameError: name 'schema' is not defined`，
# 整个 `src.api.server` 无法导入、服务端起不来。这个 bug 曾真实存在过一个版本
# （场景包重构时误加 f 前缀），而**没有任何测试导入 server**，所以全套测试一直是绿的。
# 现在由 tests/test_server_import.py 钉住。
_WRITE_SYS_PROMPT = f"""你是数据分析库的写操作助手。用户提出了一个可能涉及数据变更（增/删/改）的请求。
你的任务：判断意图并生成**提案 SQL**（{_PROFILE.dialect} 方言，当前库为 {_PROFILE.db_desc}）。

铁律：
1. 若用户真实意图是查询/分析（哪怕提到"删除""修改"等词，如"被删除的订单有哪些"），输出 intent=read，不给 SQL。
2. 写提案仅允许单条 INSERT / UPDATE / DELETE；UPDATE/DELETE 必须带 WHERE。
3. 只能操作下述业务表；严禁 DDL（CREATE/DROP/ALTER）。
4. 不确定主键/具体值时，先用保守的 WHERE（宁可 0 行也不可误伤），并在 warn 里说明。

{{schema}}

只输出 JSON（不要代码块）：
{{"intent": "write" 或 "read", "sql": "INSERT/UPDATE/DELETE 语句或空串", "warn": "给审批人看的风险说明"}}"""


def build_write_proposal(question: str) -> dict:
    """写意图判定 + 提案生成 + dry-run 预估。产出 SSE proposal 事件的全部原料。"""
    import json as _json
    from src.db.schema import TABLES
    from src.safety.writer import validate_write, dry_run_write

    schema_block = "\n".join(
        f"{t}: {m['comment']}（字段: {', '.join(m['columns'])}）" for t, m in TABLES.items())
    sys_prompt = _WRITE_SYS_PROMPT.replace("{schema}", schema_block)

    from src.core.llm import get_llm
    llm = get_llm()
    resp = llm.invoke([("system", sys_prompt), ("user", question)])
    raw = (resp.content or "").strip()
    if not raw:  # AGNES 偶发空 content——必须重试一次兜底
        resp = llm.invoke([("system", sys_prompt), ("user", question)])
        raw = (resp.content or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"intent": "read"}   # 解析不出 JSON → 按读路径走
    try:
        plan = _json.loads(m.group(0))
    except Exception:
        return {"intent": "read"}

    if plan.get("intent") != "write" or not plan.get("sql"):
        return {"intent": "read"}

    sql = str(plan["sql"]).strip().rstrip(";")
    chk = validate_write(sql)
    if not chk.passed:
        # 模型给了写 SQL 但没过三道闸：不生成提案，转读路径并记录拦截原因
        return {"intent": "read", "blocked_note": f"[写提案拦截·{chk.layer}] {chk.reason}"}

    ok, err, affected, sample = dry_run_write(str(settings.db_path), sql)
    if not ok:
        return {"intent": "read", "blocked_note": f"[dry-run 失败] {err}"}

    pid = uuid.uuid4().hex[:12]
    with _svc_conn() as con:
        con.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?)",
                    (pid, question, sql, chk.stmt_type, json.dumps(chk.tables),
                     affected, "pending", _utc(), None))
    return {"intent": "write", "proposal_id": pid, "sql": sql,
            "stmt_type": chk.stmt_type, "tables": chk.tables,
            "affected": affected, "sample": sample,
            "warn": str(plan.get("warn", ""))[:300]}


# ---------------------------------------------------------------------------
# SSE 事件归一化：两种图的节点更新 → 统一 6 类事件
# ---------------------------------------------------------------------------
def _rows_preview(rows: list, limit: int = 30) -> dict:
    return {"rows": rows[:limit], "total": len(rows),
            "truncated": len(rows) > limit}


def _norm_agentic(node: str, upd: dict) -> list[dict]:
    """agentic 图：agent（LLM 决策）→ tools（工具执行）→ respond（终态）。"""
    out: list[dict] = []
    msgs = upd.get("messages") or []
    for m in msgs:
        kind = type(m).__name__
        if kind == "AIMessage":
            calls = list(getattr(m, "tool_calls", None) or [])
            if calls:
                for c in calls:
                    name = c.get("name", "")
                    try:
                        args = (json.loads(c["args"]) if isinstance(c.get("args"), str)
                                else c.get("args") or {})
                    except Exception:
                        args = {}
                    if name == "generate_sql" and args.get("sql"):
                        out.append(_ev("sql", sql=args["sql"], step="agent:generate_sql"))
                    elif name == "execute_readonly_sql":
                        # 模型可能跳过 generate_sql 直接给 SQL 执行——同样展示
                        if args.get("sql"):
                            out.append(_ev("sql", sql=args["sql"], step="agent:execute"))
                        out.append(_ev("thought", step="agent:execute",
                                       content=f"准备执行 SQL，观察结果…"))
                    elif name == "render_chart":
                        out.append(_ev("thought", step="agent:render_chart",
                                       content=f"按结果生成图表…"))
            # 无 tool_calls 的 AIMessage 是模型自发的回答，留给 respond 节点统一吐 answer
        elif kind == "ToolMessage":
            tool = getattr(m, "name", "") or ""
            try:
                payload = json.loads(m.content or "{}")
            except Exception:
                payload = {}
            if tool == "execute_readonly_sql":
                if payload.get("ok"):
                    rows = payload.get("rows") or []
                    preview = _rows_preview(rows)
                    if payload.get("_truncated"):
                        preview["note"] = payload["_truncated"]
                    out.append(_ev("result", step="execute_readonly_sql", **preview))
                else:
                    err = payload.get("error", "执行失败")
                    blocked = bool(payload.get("blocked"))
                    out.append(_ev("error" if blocked else "thought",
                                   step="execute_readonly_sql",
                                   content=err, blocked=blocked))
            elif tool == "render_chart" and payload.get("ok"):
                out.append(_ev("chart", step="render_chart",
                               config=payload.get("chart_config")))
            elif tool == "compute_metric" and payload.get("ok"):
                out.append(_ev("thought", step="compute_metric",
                               content=f"指标 {payload.get('metric')} = {payload.get('value')}"))
            elif tool == "generate_sql":
                pass  # SQL 已在 agent 的 tool_call 阶段推送，避免重复
    if "message" in upd:
        out.append(_ev("answer", step="respond", message=_strip_chart_xml(upd["message"]),
                       status=upd.get("status", "ok"),
                       **_rows_preview(upd.get("result") or [])))
    return out


def _norm_react(node: str, upd: dict) -> list[dict]:
    """react 图：确定性管线节点逐个映射。"""
    out: list[dict] = []
    if node == "understand":
        cq = upd.get("clarified_question")
        if cq:
            out.append(_ev("thought", step="understand",
                           content=f"口径理解：{cq}"))
    elif node == "generate_sql":
        if upd.get("sql"):
            out.append(_ev("sql", step="generate_sql", sql=upd["sql"]))
    elif node == "validate_sql":
        # 拦截时 validate_sql 节点写的是 status=blocked + reject_reason（它从不写 sql_error）；
        # 只看 sql_error 会让被拦下的 SQL 也显示成「校验通过」
        blocked = upd.get("status") == "blocked"
        out.append(_ev("thought", step="validate_sql",
                       content=(f"SQL 校验未过：{upd.get('reject_reason', '')}" if blocked
                                else "SQL 校验通过（白名单/只读/LIMIT）")))
    elif node == "execute_sql":
        if upd.get("result") is not None:
            out.append(_ev("result", step="execute_sql",
                           **_rows_preview(upd["result"])))
        if upd.get("sql_error"):
            out.append(_ev("thought", step="execute_sql",
                           content=f"执行失败：{upd['sql_error']}（触发自愈）",
                           retry=upd.get("retry_count", 0)))
    elif node == "self_correct":
        if upd.get("sql"):
            out.append(_ev("sql", step=f"self_correct(第{upd.get('retry_count', 0)}次)", sql=upd["sql"]))
    elif node == "respond":
        out.append(_ev("answer", step="respond", message=_strip_chart_xml(upd.get("message", "")),
                       status=upd.get("status", "ok"),
                       **_rows_preview(upd.get("result") or [])))
    return out


def _norm_supervisor(node: str, upd: dict) -> list[dict]:
    """supervisor 图（S8）：planner/子 Agent/judge 的更新 → 统一事件。
    子 Agent 通过 emit_* 瞬态字段声明要往前端推的东西，归一化层转成事件。"""
    out: list[dict] = []
    if node == "planner" and upd.get("plan"):
        names = " → ".join(p["agent"] for p in upd["plan"])
        out.append(_ev("thought", step="planner",
                       content=f"任务规划（Supervisor 分工）：{names}"))
    if upd.get("emit_sql"):
        out.append(_ev("sql", step="sql_agent", sql=upd["emit_sql"]))
    if upd.get("emit_result") is not None:
        out.append(_ev("result", step="sql_agent", **_rows_preview(upd["emit_result"])))
    if upd.get("emit_chart"):
        out.append(_ev("chart", step="viz_agent", config=upd["emit_chart"]))
    if upd.get("emit_note"):
        out.append(_ev("thought", step=node, content=upd["emit_note"]))
    if node == "judge" and upd.get("judge_issues") and upd.get("verdict") == "fix":
        out.append(_ev("thought", step="judge",
                       content=f"问题清单：{upd['judge_issues']}"))
    if "message" in upd:
        out.append(_ev("answer", step="respond", message=_strip_chart_xml(upd["message"]),
                       status=upd.get("status", "ok"),
                       **_rows_preview(upd.get("result") or [])))
    return out


_NORM = {"agentic": _norm_agentic, "react": _norm_react, "supervisor": _norm_supervisor}

# 逐字流（stream_mode="messages"）只放行「写最终回答」的那个节点的 token；其它节点的
# LLM 输出是 JSON 计划/SQL/打分，推给前端只会是乱码。agentic 的 agent 节点也会吐
# 「边想边调工具」的中间文字，靠 delta.reset 在下一次 LLM 调用开始时清掉
_ANSWER_NODE = {"agentic": "agent", "react": "respond", "supervisor": "analysis_agent"}


# ---------------------------------------------------------------------------
# SSE 生成器（生产者线程逐事件推队列 → 异步消费者逐帧 yield，真流式）
# ---------------------------------------------------------------------------
def _stream(req: QueryRequest) -> AsyncIterator[str]:
    import asyncio
    import queue
    import threading

    graph, _cp = _get_graph(req.graph)
    rid = uuid.uuid4().hex[:12]
    # thread_id：前端每个会话固定一个（追问靠它在 checkpoint 里累积 messages），没传就
    # 每请求一个新的。同一 thread 里上一轮的计数器/终态由 _turn_input 清零，见该函数说明
    config = {"configurable": {"thread_id": req.thread_id or rid}}
    norm = _NORM[req.graph]
    q: "queue.Queue[str | None]" = queue.Queue(maxsize=128)

    def produce() -> None:
        events: list[dict] = []
        try:
            # --- P2-S9: 写意图旁路（HITL）——正则命中才试，读请求零开销 ---
            if _WRITE_HINT_RE.search(req.question):
                prop = build_write_proposal(req.question)
                if prop.get("intent") == "write":
                    ev = _ev("thought", step="write_intent",
                             content=f"识别到数据变更意图 → 生成写提案（不执行，等待人工审批）")
                    events.append(ev)
                    q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                    ev = _ev("proposal", step="hitl", **prop)
                    events.append(ev)
                    q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                    done = {"type": "done", "history_id": rid, "status": "proposal",
                            "proposal_id": prop["proposal_id"]}
                    q.put(f"data: {json.dumps(done, ensure_ascii=False)}\n\n")
                    _save_history(rid, req.question, events,
                                  f"写提案待审批：{prop['stmt_type']} 影响约 {prop['affected']} 行",
                                  "proposal", req.graph)
                    return
                if prop.get("blocked_note"):
                    ev = _ev("thought", step="write_intent",
                             content=f"写意图被安全闸拦截，转读路径处理：{prop['blocked_note']}")
                    events.append(ev)
                    q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")

            answer_node = _ANSWER_NODE[req.graph]
            last_msg_id = None
            for mode, chunk in graph.stream(_turn_input(req.graph, req.question),
                                             config=config,
                                             stream_mode=["updates", "messages"]):
                if mode == "messages":
                    msg, meta = chunk
                    text = msg.content if isinstance(msg.content, str) else ""
                    if meta.get("langgraph_node") != answer_node or not text:
                        continue
                    # 换了一次 LLM 调用（重试 / judge 打回重写 / agentic 下一轮）→ 前端清空重来；
                    # delta 不进 events：历史回放只要终态 answer，逐字碎片存进去纯属膨胀
                    ev = {"type": "delta", "text": text, "reset": msg.id != last_msg_id}
                    last_msg_id = msg.id
                    q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                    continue
                for node, upd in chunk.items():
                    if not isinstance(upd, dict):
                        continue
                    for ev in norm(node, upd):
                        events.append(ev)
                        q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
            answer_ev = next((e for e in reversed(events) if e["type"] == "answer"),
                             None)
            done = {"type": "done",
                    "history_id": rid,
                    "status": (answer_ev or {}).get("status", "ok")}
            q.put(f"data: {json.dumps(done, ensure_ascii=False)}\n\n")
            # 落盘（含事件轨迹，/api/history/{id} 可回放）
            _save_history(rid, req.question, events,
                          (answer_ev or {}).get("message", ""),
                          done["status"], req.graph)
        except Exception as e:  # noqa: BLE001 —— 任何异常都要转 error 事件兜住
            ev = _ev("error", step="runtime",
                     content=f"{type(e).__name__}: {str(e)[:200]}")
            q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
        finally:
            q.put(None)

    async def agen() -> AsyncIterator[str]:
        threading.Thread(target=produce, daemon=True).start()
        while True:
            piece = await asyncio.to_thread(q.get)
            if piece is None:
                break
            yield piece

    return agen()


@app.post("/api/query")
async def api_query(req: QueryRequest):
    if req.graph not in _NORM:
        raise HTTPException(400, "graph 必须是 agentic / react / supervisor 之一")
    return StreamingResponse(_stream(req), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/schema")
def api_schema():
    from src.db.schema import TABLES
    return {
        "tables": [
            {"name": t, "comment": meta.get("comment", ""),
             "ddl": meta.get("ddl", ""),
             "columns": [{"name": c, "comment": v} for c, v in
                         (meta.get("columns") or {}).items()]}
            for t, meta in TABLES.items()
        ]
    }


@app.get("/api/history/{rid}")
def api_history(rid: str):
    row = _load_history(rid)
    if row is None:
        raise HTTPException(404, "history not found")
    return row


@app.post("/api/feedback")
def api_feedback(fb: FeedbackRequest):
    with _svc_conn() as con:
        con.execute(
            "INSERT INTO feedback(history_id, rating, comment, created_at) VALUES (?,?,?,?)",
            (fb.history_id, fb.rating, fb.comment, _utc()))
    return {"ok": True}


# ---------------------------------------------------------------------------
# P2-S9: 写提案审批 + CSV 导入
# ---------------------------------------------------------------------------
from fastapi import UploadFile, File

from src.safety.writer import validate_write, commit_write, audit_log


def _get_proposal(pid: str) -> dict | None:
    with _svc_conn() as con:
        r = con.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def _claim_proposal(pid: str, status: str) -> bool:
    """把 pending 提案原子地改成 status，返回是否抢到。

    「先 SELECT 看是不是 pending、再执行」两步之间没有锁：两个几乎同时的确认请求
    （双击、前端重试、两个标签页）都能看到 pending，同一条写 SQL 就会被执行两次。
    改成一条带 status='pending' 条件的 UPDATE：SQLite 写锁保证只有一个请求改得动，
    另一个 rowcount=0，直接 409。
    """
    with _svc_conn() as con:
        cur = con.execute(
            "UPDATE proposals SET status=?, decided_at=? WHERE id=? AND status='pending'",
            (status, _utc(), pid))
        return cur.rowcount == 1


@app.post("/api/confirm")
def api_confirm(req: ConfirmRequest):
    """人审决策：confirm=用写凭据执行提案（事务+影响行数+审计）；reject=留痕拒绝。"""
    prop = _get_proposal(req.proposal_id)
    if prop is None:
        raise HTTPException(404, "proposal not found")

    def _conflict() -> HTTPException:
        now = (_get_proposal(req.proposal_id) or {}).get("status", "?")
        return HTTPException(409, f"提案已处理过（{now}），拒绝重复执行")

    if req.action == "reject":
        if not _claim_proposal(req.proposal_id, "rejected"):
            raise _conflict()
        audit_log(str(settings.db_path), {
            "event": "reject", "proposal_id": req.proposal_id,
            "sql": prop["sql"], "question": prop["question"]})
        return {"ok": True, "status": "rejected", "affected": 0}

    # confirm：先原子占位（executing），抢不到说明别的请求已经在处理或处理完了
    if not _claim_proposal(req.proposal_id, "executing"):
        raise _conflict()
    # 重新过三道闸（防 proposal 落库后被人篡改/库里白名单已变的边界），再执行
    chk = validate_write(prop["sql"])
    if not chk.passed:
        with _svc_conn() as con:
            con.execute("UPDATE proposals SET status='blocked', decided_at=? WHERE id=?",
                        (_utc(), req.proposal_id))
        audit_log(str(settings.db_path), {
            "event": "confirm_blocked", "proposal_id": req.proposal_id,
            "sql": prop["sql"], "reason": f"{chk.layer}: {chk.reason}"})
        raise HTTPException(422, f"提案 SQL 复检未过（{chk.layer}）：{chk.reason}")
    ok, err, affected = commit_write(str(settings.db_path), prop["sql"])
    status = "executed" if ok else "failed"
    with _svc_conn() as con:
        con.execute("UPDATE proposals SET status=?, decided_at=?, affected=? WHERE id=?",
                    (status, _utc(), affected, req.proposal_id))
    audit_log(str(settings.db_path), {
        "event": status, "proposal_id": req.proposal_id, "sql": prop["sql"],
        "affected": affected, "error": err})
    if not ok:
        raise HTTPException(500, f"执行失败：{err}")
    return {"ok": True, "status": "executed", "affected": affected,
            "sql": prop["sql"], "tables": json.loads(prop["tables"])}


@app.post("/api/upload_csv")
async def api_upload_csv(file: UploadFile = File(...)):
    """CSV 上传入库（基础版）：核心逻辑在 src/db/csvimport.py（可独立测试）。

    系统路径（非 LLM 路径）：DDL 受控生成；新表注册进白名单与 Schema 注入，
    Agent 下一次提问即可查询。
    """
    from src.db.csvimport import import_csv_bytes

    content = (await file.read()).decode("utf-8-sig", errors="replace")
    r = import_csv_bytes(file.filename or "", content, str(settings.db_path))
    if r.error:
        raise HTTPException(400, f"CSV 入库失败：{r.error}")
    audit_log(str(settings.db_path), {
        "event": "csv_import", "table": r.table,
        "rows_file": r.rows_in_file, "rows_inserted": r.rows_inserted})
    return {"ok": True, "table": r.table, "columns": r.columns, "types": r.types,
            "quality": {"rows_in_file": r.rows_in_file,
                        "rows_inserted": r.rows_inserted,
                        "match": r.quality_ok}}


@app.get("/api/health")
def health():
    return {"ok": True, "graph_cache": list(_GRAPHS.keys())}


# 静态前端（三栏页面），挂最后避免遮蔽 API 路由
app.mount("/", StaticFiles(directory=PROJECT_ROOT / "web", html=True), name="web")

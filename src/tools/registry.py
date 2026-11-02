"""
工具注册表（P2-S4 核心）。

TOOL_SCHEMAS —— 暴露给 LLM 的 JSON Schema（OpenAI function-calling 协议），
               LLM 据此输出结构化 tool_calls。
dispatch     —— 服务端分发：参数先进 Pydantic（models.PARAM_MODELS）→ 通过后调实现。
               校验失败/执行错误返回 {"ok": False, "error": ...}，由 tools 节点转成
               ToolMessage 回喂模型——模型看到错误可以自愈（ReAct 循环的 Observe 步）。

四个工具：
  generate_sql         生成 SQL（question → sql）
  execute_readonly_sql 校验+只读执行（四层安全在入口处再次触发）
  compute_metric       指标计算（sum/avg/count/mom/yoy/ratio…）
  render_chart         返回 ECharts 配置（S6 前端直接消费）
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from src.agent.fewshot import FewShotRetriever
from src.agent.prompting import build_system_prompt, render_fewshots_block, render_schema_block
from src.core.config import settings
from src.core.llm import MissingCredentialError, get_llm
from src.safety.validator import execute_with_timeout, validate_sql as run_validate
from src.tools.models import PARAM_MODELS, json_schema_to_pydantic

# ---------------- JSON Schema（给 LLM 看） ----------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "generate_sql",
            "description": "根据业务问题生成只读 SELECT 查询语句（SQLite 方言）。"
                           "模型内部已注入数据库 Schema 与历史示例。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "口径清晰的业务问题"},
                    "tables": {"type": "array", "items": {"type": "string"},
                               "description": "候选表名（可空，留空由 Schema 全量注入）"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_readonly_sql",
            "description": "对生成的 SQL 做四层安全校验（只读/表白名单/LIMIT/超时）后执行，"
                           "返回结果行数组或错误信息。只支持单条 SELECT。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "待执行的 SELECT 语句"},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_metric",
            "description": "对查询结果做二次指标计算：sum求和/avg均值/count计数/max/min/"
                           "mom环比/yoy同比/ratio占比。",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string",
                               "enum": ["sum", "avg", "count", "max", "min", "mom", "yoy", "ratio"],
                               "description": "指标类型"},
                    "data_ref": {"type": "string",
                                 "description": "结果集引用名：execute_readonly_sql 成功后的结果"
                                                "固定存于 result，直接填 \"result\""},
                    "value_col": {"type": "string", "description": "数值列名"},
                    "group_by": {"type": "array", "items": {"type": "string"},
                                 "description": "分组列名（可空）"},
                },
                "required": ["metric", "data_ref", "value_col"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_chart",
            "description": "生成 ECharts 图表配置（bar/line/pie），前端直接渲染。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string", "enum": ["bar", "line", "pie"]},
                    "x": {"type": "string", "description": "X 轴字段名"},
                    "y": {"type": "string", "description": "Y 轴字段名"},
                    "data_ref": {"type": "string",
                                 "description": "结果集引用名：execute_readonly_sql 成功后的结果"
                                                "固定存于 result，直接填 \"result\""},
                },
                "required": ["chart_type", "x", "y", "data_ref"],
            },
        },
    },
]

# ---------------- 工具实现（服务端逻辑，参数已被 Pydantic 校验） ----------------

def _tool_generate_sql(p: dict) -> dict:
    q = p["question"]
    schema_block, fewshots = _build_context(q)
    sys_prompt = build_system_prompt(schema_block=schema_block,
                                     fewshots_block=render_fewshots_block(fewshots))
    # 凭据缺失必须翻译成**结构化错误**再回喂模型，理由有两条：
    #   ① ToolMessage 是喂给模型的。SDK 原话「Missing credentials. Please pass an
    #      `api_key` … or set `OPENAI_API_KEY`」会让它解读成「这工具坏了，换一个试试」
    #      → 白烧步数；而且指向的变量名我们根本不用。
    #   ② retryable=False 是明确告诉模型别重试：缺配置不是「换个参数再来一次」能解决的。
    #      对比：安全拦截返回的 {"ok": False, "blocked": True} 隐含「换条 SQL 可以」。
    try:
        llm = get_llm()
    except MissingCredentialError as e:
        return {"ok": False,
                "error_code": "MISSING_CREDENTIALS",
                "retryable": False,
                "error": str(e).splitlines()[0].strip()}
    resp = llm.invoke([("system", sys_prompt), ("user", q)])
    raw = (resp.content or "").strip()
    # 解析：优先代码块，其次裸 SQL
    import re
    m = re.search(r"```(?:sql)?\s*(.*?)```", raw, re.S | re.I)
    sql = (m.group(1).strip() if m else raw.strip()).rstrip(";")
    return {"ok": True, "sql": sql, "question": q}


def _tool_execute_sql(p: dict) -> dict:
    sql = p["sql"]
    vr = run_validate(sql)
    if not vr.passed:
        return {"ok": False, "error": f"[安全拦截·{vr.layer}] {vr.reason}",
                "blocked": True}
    ok, err, rows = execute_with_timeout(str(settings.db_path), vr.sql)
    if not ok:
        return {"ok": False, "error": err, "blocked": False}
    return {"ok": True, "rows": rows, "row_count": len(rows)}


def _resolve_rows(registry: dict[str, Any], data_ref: str) -> list:
    """data_ref 容错解析：模型偶尔把引用名编成别名（如 top5_categories_2017），
    而注册表实际只有 result 一个键 → 取不到且非 result 时，唯一结果集下兜底用 result。"""
    rows = registry.get(data_ref) or []
    if not rows and data_ref != "result" and registry.get("result"):
        rows = registry["result"]
    return rows


def _tool_compute_metric(p: dict, results_registry: dict[str, Any]) -> dict:
    metric, value_col, group_by = p["metric"], p["value_col"], p.get("group_by") or []
    rows = _resolve_rows(results_registry, p["data_ref"])
    if not rows:
        return {"ok": False, "error": f"结果集 {p['data_ref']} 为空或不存在"}
    # 简易实现：单值聚合（分组聚合的通用实现放 S5 评测后再扩）
    vals = [r.get(value_col) for r in rows if isinstance(r.get(value_col), (int, float))]
    if not vals:
        return {"ok": False, "error": f"列 {value_col} 无非数值数据"}
    if metric == "sum":
        out = sum(vals)
    elif metric == "avg":
        out = sum(vals) / len(vals)
    elif metric == "count":
        out = len(vals)
    elif metric == "max":
        out = max(vals)
    elif metric == "min":
        out = min(vals)
    elif metric == "ratio":
        s = sum(vals)
        out = {str(rows[i].get(group_by[0])): round(v / s, 4) for i, v in enumerate(vals)} \
            if group_by and len(rows) == len(vals) else {"total_share": 1.0}
    else:  # mom / yoy 需要时间序列两期数据，先按 avg 差值近似并注明
        out = {"note": f"{metric} 需要两期数据，当前按均值 {round(sum(vals)/len(vals), 4)} 返回",
               "value": round(sum(vals) / len(vals), 4)}
    return {"ok": True, "metric": metric, "value": round(out, 4) if isinstance(out, float) else out}


def _tool_render_chart(p: dict, results_registry: dict[str, Any]) -> dict:
    rows = _resolve_rows(results_registry, p["data_ref"])
    if not rows:
        return {"ok": False, "error": f"结果集 {p['data_ref']} 为空或不存在"}
    # 列名写错时 r.get 只会取到空串和 0：图照样「画成功」，横轴空白、柱子全是 0
    missing = [c for c in dict.fromkeys((p["x"], p["y"])) if c not in rows[0]]
    if missing:
        return {"ok": False, "error": f"列 {'、'.join(missing)} 不在结果集里"
                                      f"（可用列：{'、'.join(map(str, rows[0]))}）"}
    x_vals = [str(r.get(p["x"], "")) for r in rows]
    y_vals = [r.get(p["y"], 0) for r in rows]
    cfg = {
        "tooltip": {},
        "xAxis": {"type": "category", "data": x_vals},
        "yAxis": {"type": "value"},
        "series": [{"type": p["chart_type"], "data": y_vals}],
    }
    return {"ok": True, "chart_config": cfg}


def _build_context(question: str):
    """Schema 注入 + Few-shot 检索（复用 prompting 层）。

    ⚠️ 已知重复：与 src/agent/nodes.py 的 _build_context 几乎逐行相同（S3 先有、
    S4 做工具化时复制）——差别仅在取行数/采样的连接方式（这里走 SQLAlchemy 之外
    的 sqlite3 ro 直连，语义一致）。抽公共层列为改进项；改动任一处必须同步检查另一处。
    """
    import sqlite3
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        from src.db.schema import TABLES
        row_counts, samples = {}, {}
        for t in TABLES:
            row_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            samples[t] = [tuple(r) for r in con.execute(f"SELECT * FROM {t} LIMIT 2")]
    finally:
        con.close()
    schema_block = render_schema_block(samples=samples, row_counts=row_counts)
    fewshots = FewShotRetriever().retrieve_topk(question, k=3)
    return schema_block, fewshots


# ---------------- 分发器 ----------------

def dispatch(
    name: str,
    raw_args: dict,
    results_registry: dict[str, Any] | None = None,
) -> dict:
    """统一入口：Pydantic 校验（第二道保险）→ 工具实现 → 统一 dict 结果。
    校验失败返回 {ok: False, error: '参数校验失败: ...'}，由 tools 节点转 ToolMessage。
    MCP 外部工具（S7）注册后走同一条校验通道，只是实现转发到远端会话。
    """
    results_registry = results_registry or {}
    model_cls = PARAM_MODELS.get(name)
    if model_cls is None:
        return {"ok": False, "error": f"未知工具: {name}"}
    try:
        p = model_cls(**raw_args)   # 第二道保险：类型/约束/枚举
    except ValidationError as e:
        first = e.errors()[0]
        return {"ok": False, "error": f"参数校验失败: {first.get('loc')} {first.get('msg')}"}
    pd = p.model_dump()
    if name in _EXTERNAL_DISPATCH:
        return _EXTERNAL_DISPATCH[name](pd)   # MCP 外部工具：校验后转发远端会话
    if name == "generate_sql":
        return _tool_generate_sql(pd)
    if name == "execute_readonly_sql":
        return _tool_execute_sql(pd)
    if name == "compute_metric":
        return _tool_compute_metric(pd, results_registry)
    if name == "render_chart":
        return _tool_render_chart(pd, results_registry)
    return {"ok": False, "error": f"未实现工具: {name}"}


# ---------------- MCP 外部工具注册通道（P2-S7） ----------------
# 原生四工具优先：同名 MCP 工具不注册（两者实现/安全层完全一致，注册了也是重复）；
# 非冲突的外部工具动态进 TOOL_SCHEMAS 与 PARAM_MODELS，agent 下一轮 bind_tools 即可看见。

_EXTERNAL_DISPATCH: dict[str, Any] = {}


def register_external_tool(openai_schema: dict, input_schema: dict,
                           dispatcher: Any) -> str | None:
    """注册一个 MCP 发现的外部工具。返回工具名；与原生四工具同名则跳过返回 None。"""
    name = openai_schema["function"]["name"]
    if name in PARAM_MODELS:          # 原生工具优先，拒绝覆盖
        return None
    if any(t["function"]["name"] == name for t in TOOL_SCHEMAS):
        return None
    TOOL_SCHEMAS.append(openai_schema)
    PARAM_MODELS[name] = json_schema_to_pydantic(name, input_schema)
    _EXTERNAL_DISPATCH[name] = dispatcher
    return name

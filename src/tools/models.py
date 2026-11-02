"""
四工具 Pydantic 入参模型（P2-S4，Function Calling 参数校验的「第二道保险」）。

为什么需要 Pydantic 模型（「Pydantic 双保险」）：
  - 第一道：LLM 侧用 JSON Schema（registry.TOOL_SCHEMAS）约束输出格式——但 LLM 可能
    输出缺字段/错类型/幻觉参数，schema 只是「软约束」。
  - 第二道：服务端 dispatch 前把参数实例化进 Pydantic 模型，校验失败直接拒——
    「硬校验」。模型即使生成恶意参数（如 sql 里塞 DROP），在类型/约束层就暴露。
校验策略：fail_fast（一错即抛 ValidationError），由 tools 节点捕获转 ToolMessage 回喂模型。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 允许的 SQL 关键字黑名单（参数层兜底，与 validator 的 AST 层形成纵深防御）
_BLOCKED = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "ATTACH", "PRAGMA", "INTO OUTFILE")

_SQL_MAX_LEN = 4000      # SQL 长度上限（防超长拼接）
_Q_MAX_LEN = 500         # 问题长度上限
_CHART_TYPES = ("bar", "line", "pie")   # 与 RenderChartParams 的 Literal 保持一致（scatter 走不通 dispatch）
_METRICS = ("sum", "avg", "count", "max", "min", "mom", "yoy", "ratio")


class GenerateSQLParams(BaseModel):
    """工具1: 生成 SQL（agent 先用它产出 SQL，再交给 execute 工具）。"""
    question: str = Field(..., min_length=2, max_length=_Q_MAX_LEN, description="业务问题")
    tables: list[str] = Field(default_factory=list, max_length=9, description="候选表名（可空）")

    @field_validator("tables")
    @classmethod
    def _tables_lower(cls, v):
        return [t.lower() for t in v]


class ExecuteSQLParams(BaseModel):
    """工具2: 校验后只读执行 SQL。入参即攻击面——长度/类型先拦。"""
    sql: str = Field(..., min_length=4, max_length=_SQL_MAX_LEN, description="待执行的SELECT语句")

    @field_validator("sql")
    @classmethod
    def _no_blocked_keyword(cls, v):
        up = v.upper()
        for bad in _BLOCKED:
            # 参数层粗筛（validator 的 AST 层才是权威——这里只做快速失败）
            if f" {bad} " in f" {up} " or up.startswith(bad):
                raise ValueError(f"SQL 含禁止关键字: {bad}")
        return v


class ComputeMetricParams(BaseModel):
    """工具3: 对查询结果做指标计算（环比 mom / 同比 yoy / 占比 ratio / 均值 avg…）。"""
    metric: Literal[_METRICS]  # type: ignore[arg-type]
    data_ref: str = Field(..., min_length=1, description="结果集引用名（如 result）")
    value_col: str = Field(..., min_length=1, description="数值列名")
    group_by: list[str] = Field(default_factory=list, max_length=3, description="分组列（可空）")
    window: str | None = Field(default=None, description="窗口描述（如 '按月'/'同比'）")

    @field_validator("metric")
    @classmethod
    def _metric_in(cls, v):
        if v not in _METRICS:
            raise ValueError(f"metric 必须 ∈ {_METRICS}, got {v}")
        return v


class RenderChartParams(BaseModel):
    """工具4: 生成 ECharts 配置。chart_type 必须 ∈ 白名单枚举。"""
    chart_type: Literal["bar", "line", "pie"]  # type: ignore[arg-type]
    x: str = Field(..., min_length=1, description="X 轴字段名")
    y: str = Field(..., min_length=1, description="Y 轴字段名或字段列表")
    data_ref: str = Field(..., min_length=1, description="结果集引用名")

    @field_validator("chart_type")
    @classmethod
    def _type_in(cls, v):
        if v not in _CHART_TYPES:
            raise ValueError(f"chart_type 必须 ∈ {_CHART_TYPES}, got {v}")
        return v


# 便捷构造：给 LLM 工具分发的参数入口
PARAM_MODELS = {
    "generate_sql": GenerateSQLParams,
    "execute_readonly_sql": ExecuteSQLParams,
    "compute_metric": ComputeMetricParams,
    "render_chart": RenderChartParams,
}


# ---------------------------------------------------------------------------
# JSON Schema → Pydantic 动态建模（P2-S7 MCP 外部工具注册用）
# ---------------------------------------------------------------------------

_JSON_TYPES = {"string": str, "number": float, "integer": int,
               "boolean": bool, "array": list, "object": dict}


def json_schema_to_pydantic(name: str, schema: dict) -> type:
    """把 MCP tool 的 inputSchema 转成 Pydantic 模型。

    为什么必须过这一层：外部 MCP 工具注册进 agent 后，走的是与原生四工具同一条
    dispatch 通道——参数先过 Pydantic 再转发，**外部工具不旁路校验**：
    工具可以外挂，安全语义不外挂。
    """
    from typing import Any

    from pydantic import create_model

    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, tuple] = {}
    for fname, spec in props.items():
        py_t = _JSON_TYPES.get((spec or {}).get("type"), Any)
        if fname in required:
            fields[fname] = (py_t, ...)
        else:
            default = (spec or {}).get("default")
            fields[fname] = (py_t | None, default) if default is None else (py_t, default)
    return create_model(f"{name}_args", **fields)

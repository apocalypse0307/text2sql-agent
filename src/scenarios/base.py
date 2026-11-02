"""ScenarioProfile 数据类：一个业务场景的全部「口径级」参数。

字段取舍原则：只收「多个 prompt 共用、且随业务变化」的值——
单处使用或纯模板措辞留在各 prompt 里，避免过度抽象。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioProfile:
    name: str              # 场景包名（日志/文档标识）
    domain: str            # 领域名词（如「电商」），进人设句「你是 XX 数据分析…」
    dialect: str           # SQL 方言（prompt 明示，必须与执行引擎一致）
    known_max_year: int    # 相对时间（最近/今年）的兜底口径年份
    data_range: str        # 数据范围描述（进 planner/judge/空结果提示）
    table_catalog: str     # 业务表的斜杠枚举（多个 prompt 复用，改表只改这里）
    default_status: str    # 订单/记录的默认状态口径（如 delivered）
    dedup_key: str         # 去重/复购统计用的键（如 customer_unique_id）
    currency: str          # 币种口径（如 BRL，prompt 明示不换算）
    db_desc: str           # 数据库一句话描述（写提示词的「当前库为 …」）


def get_profile() -> ScenarioProfile:
    """当前激活场景。轻量版固定返回 Olist；接多场景时在这里按配置切换。"""
    from src.scenarios.olist import OLIST
    return OLIST

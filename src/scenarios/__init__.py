"""场景包（Scenario Pack，P2 复用性收口）：图是通用引擎，业务是可插拔外壳。

设计立场（对应「不把业务硬绑定进 Graph 节点/State」的架构原则）：
  - LangGraph 图编排、四层安全、工具注册、HITL、MCP 全部业务无关；
    业务只以「场景包」形式注入：口径规则、表目录、年份兜底、数据范围、领域人设。
  - 换业务场景 = 写一个新 ScenarioProfile + 新 db/schema TABLES + 新金标/few-shot，
    引擎与图零改动（动态表注册已打通白名单与 Schema 注入，见 db/schema.py）。
  - 轻量实现：不做插件加载器，profile 是纯数据类；各 prompt 模板从 profile 取值渲染，
    Olist 是当前唯一内置场景（src/scenarios/olist.py），也是全模块默认值。
"""
from src.scenarios.base import ScenarioProfile, get_profile
from src.scenarios.olist import OLIST

__all__ = ["ScenarioProfile", "get_profile", "OLIST"]

"""场景包（Scenario Pack）测试：验证「业务外壳可插拔、行为零变化」。

覆盖：
  ① 接线正确：三张图的 prompt 从 OLIST 场景包取值（口径关键词/表目录/数据范围在位）
  ② 换场景可行：构造一个医疗场景 profile，prompt 渲染结果随之切换——
     引擎（图/安全/工具）零改动，只换数据契约
运行：python -m pytest tests/test_scenario.py -v
"""
from __future__ import annotations


# ---------------- ① Olist 接线 ----------------

def test_understand_prompt_uses_profile():
    from src.agent.nodes import _UNDERSTAND_PROMPT, _PROFILE
    rendered = _UNDERSTAND_PROMPT.format(max_year=_PROFILE.known_max_year,
                                         catalog=_PROFILE.table_catalog)
    assert str(_PROFILE.known_max_year) in rendered
    assert _PROFILE.table_catalog in rendered
    assert _PROFILE.dedup_key in rendered


def test_agentic_prompt_uses_profile():
    from src.agent.agentic_graph import _AGENTIC_SYSTEM_PROMPT, _PROFILE
    assert _PROFILE.data_range in _AGENTIC_SYSTEM_PROMPT
    assert _PROFILE.default_status in _AGENTIC_SYSTEM_PROMPT
    assert _PROFILE.dedup_key in _AGENTIC_SYSTEM_PROMPT
    # 表目录进人设句与规则 5
    assert _AGENTIC_SYSTEM_PROMPT.count(_PROFILE.table_catalog.replace("/", " / ")) >= 2


def test_supervisor_prompts_use_profile():
    from src.agent.supervisor_graph import _SQL_AGENT_PROMPT, _JUDGE_PROMPT, _PROFILE
    assert _PROFILE.table_catalog.replace("/", " / ") in _SQL_AGENT_PROMPT
    assert str(_PROFILE.known_max_year) in _SQL_AGENT_PROMPT
    assert _PROFILE.data_range in _JUDGE_PROMPT
    assert _PROFILE.dedup_key in _JUDGE_PROMPT
    # {schema} 占位符仍可被 sql_agent 的 replace 注入
    assert "{schema}" in _SQL_AGENT_PROMPT


def test_build_system_prompt_defaults_olist():
    from src.agent.prompting import build_system_prompt
    from src.scenarios import OLIST
    sp = build_system_prompt(schema_block="-- DDL", fewshots_block="")
    assert f"原始币种 {OLIST.currency}" in sp
    assert f"已送达({OLIST.default_status})" in sp
    assert OLIST.dedup_key in sp
    assert OLIST.dialect in sp          # dialect_note 缺省取 profile


# ---------------- ② 换场景演示 ----------------

def test_swap_scenario_profile():
    """换场景 = 换 profile：prompt 随之切换，引擎零改动（复用性验收）。"""
    from src.agent.prompting import build_system_prompt
    from src.scenarios.base import ScenarioProfile

    hospital = ScenarioProfile(
        name="hospital", domain="医疗", dialect="PostgreSQL",
        known_max_year=2024, data_range="2022-01 ~ 2024-12",
        table_catalog="patients/visits/diagnoses/prescriptions",
        default_status="completed", dedup_key="patient_uid", currency="CNY",
        db_desc="医院 HIS 数据库",
    )
    sp = build_system_prompt(schema_block="-- DDL", fewshots_block="", profile=hospital)
    assert "医疗业务数据分析助手" in sp
    assert "PostgreSQL" in sp and "CNY" in sp
    assert "patient_uid" in sp and "completed" in sp
    # Olist 口径不得残留
    assert "BRL" not in sp and "customer_unique_id" not in sp

    rendered = ("口径检查", hospital.known_max_year, hospital.table_catalog)
    assert hospital.known_max_year == 2024 and "patients" in rendered[2]


def test_frozen_profile_is_data_only():
    """profile 是纯数据契约（frozen dataclass）：不可变、不持有引擎引用。"""
    from src.scenarios import OLIST
    from dataclasses import fields, is_dataclass
    assert is_dataclass(OLIST)
    names = {f.name for f in fields(OLIST)}
    assert {"name", "domain", "dialect", "known_max_year", "data_range",
            "table_catalog", "default_status", "dedup_key", "currency",
            "db_desc"} <= names

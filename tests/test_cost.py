"""成本计量与预算熔断的单测（零 LLM 消耗）。

为什么这个模块必须有测试：它产出的数字会直接写进 METRICS/README。
「花了几毛钱」如果算错，是所有指标里最难被外部发现的一类
—— 别人问「这 0.18 元怎么算的」，要能指到这一行断言。

覆盖：
  ① 计价算术：手算值与 CostTracker 的累计值逐位对齐（含峰谷两档）
  ② 缓存命中四写法：DeepSeek 原生 / OpenAI 原生 / LangChain 两种嵌套位置都要认得出
     （漏读不报错，但会把输入按未命中计价 → 贵 50 倍 → 预算被高估）
  ③ 预算熔断：超上限抛 BudgetExceeded
  ④ 峰谷判定：工作日 9-12/14-18 为峰，周末全谷
  ⑤ usage 提取：llm_output 传统位置 与 message.usage_metadata 新位置都能取到
"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.core.cost import (CN_TZ, DEEPSEEK_FLASH_PRICING, BudgetExceeded,
                           CostTracker, _cache_hit_tokens, _extract_usage, is_peak)


class _Msg:
    def __init__(self, usage_metadata=None):
        self.usage_metadata = usage_metadata


class _Gen:
    def __init__(self, message=None):
        self.message = message


class _FakeResult:
    """最小 LLMResult 替身：只要能喂给 _extract_usage 就够了。"""

    def __init__(self, token_usage=None, usage_metadata=None):
        self.llm_output = {"token_usage": token_usage} if token_usage else {}
        self.generations = [[_Gen(_Msg(usage_metadata))]]


def _expected_cny(prompt, hit, out, peak: bool) -> float:
    """独立算一遍（与被测实现同式但分开写，避免「用实现验证实现」）。"""
    p = DEEPSEEK_FLASH_PRICING["peak" if peak else "off_peak"]
    miss = prompt - hit
    return hit / 1e6 * p["cache_hit"] + miss / 1e6 * p["cache_miss"] + out / 1e6 * p["output"]


# ---------------- ① 计价算术 ----------------

def test_cost_arithmetic_matches_hand_calc(monkeypatch):
    """100万 输入（其中 80 万命中）+ 10万 输出 —— 手算对齐。"""
    peak = is_peak()
    t = CostTracker()
    res = _FakeResult(token_usage={
        "prompt_tokens": 1_000_000, "completion_tokens": 100_000,
        "prompt_cache_hit_tokens": 800_000,
    })
    t.on_llm_end(res)

    assert t.calls == 1
    assert (t.input_tokens, t.cache_hit_tokens, t.output_tokens) == (1_000_000, 800_000, 100_000)
    assert t.cost_cny == pytest.approx(_expected_cny(1_000_000, 800_000, 100_000, peak))
    assert t.cache_hit_rate == pytest.approx(0.8)


def test_tracker_picks_peak_or_offpeak_branch(monkeypatch):
    """同一个 usage，走峰档与谷档的计价必须差 2 倍。

    ★ 必须 monkeypatch 模块里的 is_peak 来驱动分支——直接读价格表重算是**同义反复**
      （只验证了常量，没验证被测代码真的用了那个分支）。
      这条是「要不要等到 18:00 之后再跑评测」这个决策的依据，得真测。
    """
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 100_000,
             "prompt_cache_hit_tokens": 0}

    monkeypatch.setattr("src.core.cost.is_peak", lambda *a, **k: True)
    t_peak = CostTracker()
    t_peak.on_llm_end(_FakeResult(token_usage=dict(usage)))

    monkeypatch.setattr("src.core.cost.is_peak", lambda *a, **k: False)
    t_off = CostTracker()
    t_off.on_llm_end(_FakeResult(token_usage=dict(usage)))

    assert t_peak.cost_cny == pytest.approx(t_off.cost_cny * 2)
    # 与手算值对齐（峰档：输入 2.0/M、输出 8.0/M）
    assert t_peak.cost_cny == pytest.approx(1 * 2.0 + 0.1 * 8.0)


# ---------------- ② 缓存命中四写法 ----------------

@pytest.mark.parametrize("usage", [
    {"prompt_cache_hit_tokens": 900},                              # DeepSeek 原生（顶层）
    {"cache_read_input_tokens": 900},                              # Anthropic 风格
    {"prompt_tokens_details": {"cached_tokens": 900}},             # OpenAI 原生
    {"input_token_details": {"cache_read": 900}},                  # LangChain 归一化
])
def test_cache_hit_four_spellings_all_detected(usage):
    """四个键名写法都必须认得出。漏一个 → 命中恒 0 → 输入全按未命中计价（贵 50 倍）。"""
    assert _cache_hit_tokens(usage, 1000) == 900


def test_cache_hit_absent_returns_zero():
    assert _cache_hit_tokens({"prompt_tokens": 1000}, 1000) == 0


def test_cache_hit_cannot_exceed_prompt_tokens():
    """脏数据防御：命中数 > 输入数时被夹住，否则 miss 会变负数、成本算反。"""
    t = CostTracker()
    peak = is_peak()
    t.on_llm_end(_FakeResult(token_usage={
        "prompt_tokens": 100, "completion_tokens": 0,
        "prompt_cache_hit_tokens": 99_999,
    }))
    assert t.cache_hit_tokens == 100
    assert t.cost_cny == pytest.approx(_expected_cny(100, 100, 0, peak))


# ---------------- ③ 预算熔断 ----------------

def test_budget_exceeded_raises():
    t = CostTracker(cap_cny=0.001)
    with pytest.raises(BudgetExceeded) as ei:
        t.on_llm_end(_FakeResult(token_usage={
            "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
        }))
    assert "已超过上限" in str(ei.value)


def test_no_cap_never_raises():
    t = CostTracker(cap_cny=None)
    for _ in range(5):
        t.on_llm_end(_FakeResult(token_usage={"prompt_tokens": 100_000,
                                              "completion_tokens": 100_000}))
    assert t.calls == 5 and t.cost_cny > 0


# ---------------- ④ 峰谷判定 ----------------

@pytest.mark.parametrize("hour,weekday,expect_peak", [
    (9, 0, True), (11, 4, True), (12, 0, False), (14, 2, True),
    (17, 0, True), (18, 0, False), (3, 1, False), (10, 5, False), (10, 6, False),
])
def test_is_peak_table(hour, weekday, expect_peak):
    """周一至周五 9-12、14-18 为峰；周末全谷。"""
    # 2026-10-12 是周一
    day = 12 + weekday
    assert is_peak(datetime(2026, 10, day, hour, 30, tzinfo=CN_TZ)) is expect_peak


# ---------------- ⑤ usage 提取两处位置 ----------------

def test_extract_usage_from_llm_output():
    u = _extract_usage(_FakeResult(token_usage={"prompt_tokens": 7, "completion_tokens": 3}))
    assert u["prompt_tokens"] == 7 and u["completion_tokens"] == 3


def test_extract_usage_from_message_metadata():
    u = _extract_usage(_FakeResult(usage_metadata={
        "input_tokens": 11, "output_tokens": 5,
        "input_token_details": {"cache_read": 4},
    }))
    assert u["prompt_tokens"] == 11 and u["completion_tokens"] == 5
    assert _cache_hit_tokens(u, 11) == 4


def test_extract_usage_normalizes_langchain_input_tokens_key():
    """LangChain 归一化 dict 用 input_tokens/output_tokens 而非 prompt_tokens——要归一化。"""
    u = _extract_usage(_FakeResult(token_usage={"input_tokens": 20, "output_tokens": 2}))
    assert u["prompt_tokens"] == 20 and u["completion_tokens"] == 2


def test_extract_usage_empty_is_safe():
    """拿不到 usage 时返回空 dict —— 调用方必须能静默跳过，不能崩。"""
    assert _extract_usage(_FakeResult()) == {}
    t = CostTracker()
    t.on_llm_end(_FakeResult())          # 不抛异常
    assert t.calls == 0


# ---------------- 报告与序列化 ----------------

def test_report_and_as_dict_consistent():
    t = CostTracker()
    t.on_llm_end(_FakeResult(token_usage={"prompt_tokens": 1000, "completion_tokens": 500,
                                          "prompt_cache_hit_tokens": 800}))
    d = t.as_dict()
    assert d["calls"] == 1 and d["input_tokens"] == 1000 and d["cache_hit_tokens"] == 800
    assert d["cache_hit_rate"] == pytest.approx(0.8)
    assert d["cost_cny"] == pytest.approx(t.cost_cny)
    assert "实测成本" in t.report()

"""LLM 成本计量与预算熔断（P2 版，与 P1 agentic-rag-finance 同构）。

用途：把「这一轮跑了多少钱」从估算变成**实测**，并在超预算前主动熔断，
      避免跑到一半余额耗尽导致整轮作废（本项目的评测动辄 60 条 × 多次自愈重试，
      比 P1 更容易把额度一次烧完）。

原理：OpenAI 兼容接口在响应里返回 usage（含「缓存命中/未命中」细分），
      用 LangChain 的 BaseCallbackHandler 在 on_llm_end 里汇总即可 ——
      **不改任何业务代码**，只往 graph.invoke 的 config 里挂一个回调。

为什么必须区分「缓存命中/未命中」：DeepSeek 输入价按二者分档，差价 50 倍
（实测价：命中 ￥0.04/M vs 未命中 ￥2/M 高峰）。P2 的 system prompt（schema 注入 +
few-shot）每条都一模一样，是天然的高命中场景；若一律按未命中估价，
预算会**高估几十倍**，从而得出「跑不起」的错误结论。

注意（估值口径的诚实说明）：高峰/空闲按北京时间「周一至周五 9:00-12:00、14:00-18:00」
判定，**未接入中国法定节假日表** —— 节假日本应算空闲时段，故本模块在节假日会
**高估**成本。方向安全（宁可少跑），但不要拿它当对账单。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from langchain_core.callbacks import BaseCallbackHandler

CN_TZ = timezone(timedelta(hours=8))

# DeepSeek 官方计价（元 / 百万 tokens），deepseek-flash = DeepSeek-V4.1-Flash
# 来源：api-docs.deepseek.com/zh-cn/quick_start/pricing
DEEPSEEK_FLASH_PRICING = {
    "peak": {"cache_hit": 0.04, "cache_miss": 2.0, "output": 8.0},
    "off_peak": {"cache_hit": 0.02, "cache_miss": 1.0, "output": 4.0},
}


class BudgetExceeded(RuntimeError):
    """累计成本超过上限时抛出——由调用方决定是中止整轮还是降级继续。"""


def is_peak(now: datetime | None = None) -> bool:
    """当前是否处于高峰时段（北京时间周一至周五 9-12、14-18）。

    用途：选对计价档位。节假日未接入 → 会高估（见模块 docstring）。
    ★ 实测价差 2 倍，所以「要不要等到 18:00 之后再跑」是个真实可算的决策。
    """
    now = now or datetime.now(CN_TZ)
    if now.weekday() >= 5:               # 周六周日全天空闲
        return False
    h = now.hour
    return (9 <= h < 12) or (14 <= h < 18)


@dataclass
class CostTracker(BaseCallbackHandler):
    """挂在 LangChain config 上的成本累加器（线程内使用，非线程安全）。

    用法：
        tracker = CostTracker(cap_cny=3.0)
        st = answer(graph, q, thread_id=..., callbacks=[tracker])
        print(tracker.report())
    """

    model: str = "deepseek-flash"
    cap_cny: float | None = None
    pricing: dict = field(default_factory=lambda: DEEPSEEK_FLASH_PRICING)

    # ---- 累计量（全部为实测值）----
    calls: int = 0
    input_tokens: int = 0          # 总输入（含命中与未命中）
    cache_hit_tokens: int = 0
    output_tokens: int = 0
    cost_cny: float = 0.0

    # ---- 明细留痕（便于事后核对是哪一步烧的钱）----
    per_call: list = field(default_factory=list)

    # ---------- LangChain 回调 ----------
    def on_llm_end(self, response, **kwargs) -> None:
        u = _extract_usage(response)
        if not u:
            return
        prompt = int(u.get("prompt_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
        if not prompt and not completion:
            return

        hit = _cache_hit_tokens(u, prompt)
        hit = min(hit, prompt)
        miss = int(u.get("prompt_cache_miss_tokens") or (prompt - hit))

        p = self.pricing["peak" if is_peak() else "off_peak"]
        c = (hit / 1e6 * p["cache_hit"]
             + miss / 1e6 * p["cache_miss"]
             + completion / 1e6 * p["output"])

        self.calls += 1
        self.input_tokens += prompt
        self.cache_hit_tokens += hit
        self.output_tokens += completion
        self.cost_cny += c
        self.per_call.append({"in": prompt, "hit": hit, "out": completion, "cny": round(c, 6)})

        if self.cap_cny is not None and self.cost_cny > self.cap_cny:
            raise BudgetExceeded(
                f"累计成本 ￥{self.cost_cny:.4f} 已超过上限 ￥{self.cap_cny:.2f}"
                f"（{self.calls} 次调用 / 输入 {self.input_tokens} / 输出 {self.output_tokens}）")

    # ---------- 读取 ----------
    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hit_tokens / self.input_tokens if self.input_tokens else 0.0

    def report(self) -> str:
        return (f"LLM 调用 {self.calls} 次 | 输入 {self.input_tokens} tok"
                f"（缓存命中 {self.cache_hit_tokens} = {self.cache_hit_rate:.1%}）"
                f" | 输出 {self.output_tokens} tok | 实测成本 ￥{self.cost_cny:.4f}"
                f"（{'高峰' if is_peak() else '空闲'}价）")

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "output_tokens": self.output_tokens,
            "cost_cny": round(self.cost_cny, 6),
            "peak": is_peak(),
            "model": self.model,
        }


def _cache_hit_tokens(usage: dict, prompt_tokens: int) -> int:
    """从 usage 里挖出「缓存命中的输入 token 数」。

    ★ 键名有四种写法，踩过：不同层级的归一化各用各的名字——
      · DeepSeek 原生 API  ： prompt_cache_hit_tokens（顶层）
      · OpenAI 原生 API    ： prompt_tokens_details.cached_tokens
      · LangChain usage_metadata： input_token_details.cache_read
      · LangChain llm_output 的归一化 dict： input_token_details.cache_read
    漏读的后果不是报错，而是**缓存命中恒为 0** → 全部输入按未命中计价（贵 50 倍）
    → 预算被高估、可能因此不敢跑；所以四个都试一遍。
    """
    for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens"):
        if usage.get(key):
            return int(usage[key])
    for details_key in ("prompt_tokens_details", "input_token_details"):
        d = usage.get(details_key) or {}
        for key in ("cached_tokens", "cache_read"):
            if d.get(key):
                return int(d[key])
    return 0


def _extract_usage(response) -> dict:
    """从 LLMResult 里挖出 usage。

    三条路子都试（不同版本/提供方放的位置不同）：
      ① response.llm_output["token_usage"]  —— langchain_openai 传统位置
      ② 其归一化子键 input_token_details（缓存命中在这）
      ③ 最后一条消息的 usage_metadata        —— 新版标准化位置
    """
    llm_out = getattr(response, "llm_output", None) or {}
    u = llm_out.get("token_usage") or {}
    if u:
        if not u.get("prompt_tokens") and u.get("input_tokens") is not None:
            u = dict(u, prompt_tokens=u.get("input_tokens", 0),
                     completion_tokens=u.get("output_tokens", 0))
        return u
    try:
        gen = response.generations[0][0]
        msg = getattr(gen, "message", None)
        um = getattr(msg, "usage_metadata", None) or {}
        if um:
            return {
                "prompt_tokens": um.get("input_tokens", 0),
                "completion_tokens": um.get("output_tokens", 0),
                "prompt_tokens_details": um.get("input_token_details", {}) or {},
            }
    except (IndexError, AttributeError):
        pass
    return {}

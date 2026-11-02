"""
LLM 客户端工厂（P2 版，镜像 P1 同构设计）。

用途：全项目唯一 LLM 实例来源，通过 settings.llm_provider 切换 Agnes / DeepSeek。
原理：两家都实现 OpenAI 协议，统一用 langchain_openai.ChatOpenAI；temperature=0 保证
      SQL 生成确定性（SQL 生成场景温度非 0 会导致同样的问句两次生成不同 SQL）。
"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from src.core.config import settings


class MissingCredentialError(RuntimeError):
    """当前 LLM provider 的凭据（或 provider 取值）缺失——**部署期错误**。

    为什么要单独一个异常类型：凭据缺失是「部署没配好」，不是「模型这次答得不好」。
    它必须能被**响亮地**分辨出来，否则会被 `answer*` 的 `except Exception` 兜成
    `status="degraded"`，用户看到的是「Agent 好像不太会写 SQL」，而不是
    「你少配了一个变量」——报错报在错的地方，比不报错更难查。

    为什么要自己造而不是让 SDK 抛：`openai.OpenAIError` 的原话是
    「Missing credentials. Please pass an `api_key` … or set the `OPENAI_API_KEY`
    environment variable」——**把我们的人指向了一个本项目根本没用的变量名**
    （我们是 `DEEPSEEK_API_KEY` / `AGNES_API_KEY`）。报错文案必须点名到真实变量。
    """


# provider → (Settings 字段名, 环境变量名)
# 两张表必须同步：字段名给代码读，环境变量名给报错文案用（要告诉人去配哪一个）。
_PROVIDER_CREDENTIAL: dict[str, tuple[str, str]] = {
    "deepseek": ("deepseek_api_key", "DEEPSEEK_API_KEY"),
    "agnes": ("agnes_api_key", "AGNES_API_KEY"),
}


def _fix_hint(field: str, env_name: str) -> str:
    """报错文案：缺哪个变量、在哪配、怎么验证——三步都要能照着做。"""
    return (
        f"\n  修法（二选一）：\n"
        f"    ① 项目根 .env 里写一行：  {env_name}=<你的 key>\n"
        f"    ② 设为用户级环境变量（Windows）：setx {env_name} \"<你的 key>\"（新开终端生效）\n"
        f"  验证：python -c \"from src.core.config import settings as s; "
        f"print(s.llm_provider, bool(s.{field}))\""
    )


def require_llm_credentials() -> None:
    """启动期体检：当前 provider 的凭据必须就位，否则抛 `MissingCredentialError`。

    放在**连接 / 建图之前**调用，让部署错误在启动时暴露，而不是等到第一次
    `generate_sql` 才炸。取值一律走 `settings`（它已合并 .env 与进程环境）。
    """
    provider = (settings.llm_provider or "").strip().lower()
    pair = _PROVIDER_CREDENTIAL.get(provider)
    if pair is None:
        raise MissingCredentialError(
            f"LLM_PROVIDER='{settings.llm_provider}' 不是已知的 provider，"
            f"可选值：{' / '.join(sorted(_PROVIDER_CREDENTIAL))}。"
            f"\n  注意：不做「猜一个默认 provider 继续跑」的兜底——"
            f"配错了却静默跑另一家，比直接失败难查得多。"
        )
    field, env_name = pair
    if not str(getattr(settings, field, "") or "").strip():
        raise MissingCredentialError(
            f"缺少 LLM 凭据：provider='{provider}' 需要 {env_name}，"
            f"但 settings.{field} 读到的是空值。" + _fix_hint(field, env_name)
        )


def has_llm_credentials() -> bool:
    """只判断、不抛：当前 provider 的凭据是否就位（供测试/运维脚本区分失败原因）。

    存在的意义是把「环境没配」和「配置没传进去」区分开：
      · False → 这条用例的前置条件不成立，应 skip，而不是报红；
      · True 但下游仍报缺凭据 → **必是传参层 bug**（白名单漏项、子进程环境没给），必须红。
    映射表与 `require_llm_credentials()` 共用同一份，避免「两处各写一遍、总有一天不同步」。
    """
    provider = (settings.llm_provider or "").strip().lower()
    pair = _PROVIDER_CREDENTIAL.get(provider)
    if pair is None:
        return False
    field, _env_name = pair
    return bool(str(getattr(settings, field, "") or "").strip())


def _deepseek_extra_body() -> dict:
    """DeepSeek 思考模式开关（OpenAI 格式要求放在 extra_body 里）。

    本项目**必须关掉**（见 config 里 deepseek_thinking 的注释）：
    最硬的理由是带 tools 的请求在思考模式下要求回传 reasoning_content，否则 400，
    而 LangChain 的工具调用循环不会替我们回传 —— 开着它，所有工具类节点都会挂。
    """
    mode = (settings.deepseek_thinking or "").strip().lower()
    if mode in ("", "default"):
        return {}
    return {"thinking": {"type": mode}}


def get_llm(*, streaming: bool = False) -> BaseChatModel:
    # 先过体检：缺凭据时抛我们自己的 MissingCredentialError（文案点名到真实变量），
    # 而不是让 ChatOpenAI 去抛 openai.OpenAIError（文案指向我们没用到的 OPENAI_API_KEY）
    require_llm_credentials()
    provider = settings.llm_provider
    # 只在要 True 时才传：显式 streaming=False 会被 langchain 当成「硬关流式」，
    # API 的 stream_mode="messages" 就再也拿不到逐 token 回调，只能整段吐
    stream_kw = {"streaming": True} if streaming else {}
    if provider == "deepseek":
        extra = _deepseek_extra_body()
        return ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
            **stream_kw,
            # 自定义 base_url 时 langchain 默认不要流式 usage；API 走 stream_mode="messages"
            # 会让节点里的 invoke 也变成流式调用，不显式打开就只剩按字符估算的兜底
            stream_usage=True,
            **({"extra_body": extra} if extra else {}),
        )
    # 默认 agnes。max_retries=1：SDK 内置指数退避在 429 时会拖 10-20 分钟
    # （S8 双跑实测踩坑），重试职责上移给调用层（_llm_json 20s 退避 / 评测层 60s 退避）
    return ChatOpenAI(
        model=settings.agnes_model,
        api_key=settings.agnes_api_key,
        base_url=settings.agnes_base_url,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
        **stream_kw,
        max_retries=1,
    )

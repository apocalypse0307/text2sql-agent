"""
全局配置模块（P2 Text2SQL 版）。

与 P1 共用同一套 config 心智模型：项目唯一配置入口、pydantic-settings 自动读环境变量、
所有产物路径锚定项目内。区别：P2 不加载本地向量/精排模型（SQL Agent 不需要），
所以这里没有 embed/rerank 配置块——Few-shot 示例检索用轻量方案（见 src/agent/fewshot.py）。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：src/core/config.py -> parents[0]=core, [1]=src, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- LLM 提供方切换（与 P1 同构）----------
    llm_provider: str = "agnes"            # agnes | deepseek

    # Agnes（OpenAI 兼容；AGNES_API_KEY 已配置在用户级环境变量）
    agnes_api_key: str = ""
    agnes_base_url: str = "https://apihub.agnes-ai.com/v1"
    agnes_model: str = "agnes-2.0-flash"

    # DeepSeek（OpenAI 协议兼容；DEEPSEEK_API_KEY 已配置）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.0           # SQL 生成要确定性，温度 0
    llm_max_tokens: int = 4096             # agnes 需 >=256，过小会整段空
    llm_timeout: int = 120
    # DeepSeek-V4 起「思考模式」默认开启（effort=high）。本项目**必须关掉**，两个硬理由：
    #   ① 本项目大量用 bind_tools / tool_calls。思考模式下带 tools 的请求要求把
    #      reasoning_content 完整回传，否则 API 直接返回 400；而 LangChain 的工具
    #      调用循环不会替我们回传 → 工具类节点必挂。
    #   ② 思维链 token 计入**输出**计费（成本高数倍），且会吃掉 max_tokens 预算
    #      （与 Agnes 那个「过小则 content 整段空」同款坑）。
    # 取值：disabled | enabled（想看思考模式对 SQL 生成准确率的增益时改这项）
    deepseek_thinking: str = "disabled"

    # ---------- 评测用金标库 / Few-shot 库路径 ----------
    goldset_path: str = str(PROJECT_ROOT / "evals" / "goldset.json")
    fewshots_path: str = str(PROJECT_ROOT / "evals" / "few_shots.json")

    # ---------- 数据库（SQLAlchemy URL 抽象：SQLite 开发 / MySQL 生产只改这一行）----------
    # 安全第一层「只读账号」：SQLite 开发期用 mode=ro URI 等价实现（见 src/db/connect.py）
    db_url: str = "sqlite:///" + str(PROJECT_ROOT / "data" / "db" / "olist.db").replace("\\", "/")
    db_ro_uri: str = "file:" + str(PROJECT_ROOT / "data" / "db" / "olist.db").replace("\\", "/") + "?mode=ro"

    # ---------- 路径 ----------
    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def db_path(self) -> Path:
        return PROJECT_ROOT / "data" / "db" / "olist.db"


# 全局单例：其他模块 `from src.core.config import settings`
settings = Settings()

"""
Prompt 渲染模块（P2-S2 Schema 注入 + Few-shot 检索的 Prompt 侧）。

用途：
  - render_schema_block()   把 TABLES 渲染成给 LLM 的 Schema 注入文本（「注释即知识」落地）
  - render_fewshots_block() 把命中的 few-shot 示例渲染成示例块
  - build_system_prompt()   组装最终 System Prompt

原理：
  1. Schema 注入 ≠ 丢 DDL。真正提升准确率的是**每列的中文注释**——LLM 从注释里知道
     order_status 的枚举值、price 的币种、复购分析该用 customer_unique_id 而不是
     customer_id。这就是「Schema 注入」的含义。
  2. 注释以 `-- 行内注释` 拼进 CREATE TABLE：列名与解释零距离对齐，比单独一张注释表
     更省 token 也更不易看串行。渲染结果仍是可执行的合法 SQLite DDL。
  3. 附真实采样行（INSERT 注释形式）：模型据此判断数据格式（时间戳是 '2017-10-02
     10:56:33'、金额是小数、state 是两位缩写），避免臆造格式导致类型错误。
  4. DDL 用 SQLite 方言与 dev 库一致——LLM 生成的 SQL 与执行引擎方言永远一致，
     不会出现「生成 MySQL 语法、本地 SQLite 执行报错」的方言错配。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

from src.db.schema import RELATIONS, TABLES

if TYPE_CHECKING:
    # 仅用于注解（本模块有 `from __future__ import annotations`，注解不参与运行时求值）。
    # 不放运行期导入是为了避免 prompting ← scenarios ← prompting 的循环导入风险；
    # 但**必须有**这条 TYPE_CHECKING 导入，否则 `"ScenarioProfile | None"` 是悬空引用，
    # ruff F821 与 mypy 都会报，且 IDE 跳转失效。
    from src.scenarios.base import ScenarioProfile

# 匹配列定义行：允许行首缩进，行首是列名（跳过 PRIMARY KEY (...)、) 等约束行）
_COL_RE = re.compile(r"^\s*(\w+)\s")


def _annotate_ddl(name: str, columns: dict[str, str]) -> str:
    """把中文注释行内拼进 CREATE TABLE DDL（保留可执行性）。"""
    raw = TABLES[name]["ddl"].strip().splitlines()
    out: list[str] = []
    for line in raw:
        m = _COL_RE.match(line)
        col = m.group(1) if m else None
        if col in columns:
            # 原行可能以逗号结尾；注释追加在行尾（-- 注释到行尾，不影响前面的列定义）
            line = f"{line}  -- {columns[col]}"
        out.append(line)
    return "\n".join(out)


def render_schema_block(
    tables: Iterable[str] | None = None,
    samples: dict[str, list[tuple]] | None = None,
    row_counts: dict[str, int] | None = None,
) -> str:
    """渲染 Schema 注入块。tables=None 表示全量注入（9 表 DDL 仅 ~5KB，实测全量注入
    就是准确率最优解，无需剪枝——大库场景的表选择已由 understand/generate 的
    few-shot 检索隐式引导，本函数保留 tables 参数即为将来接入大库预留的剪枝口）。"""
    names = list(tables) if tables else list(TABLES)
    blocks: list[str] = []
    for name in names:
        t = TABLES[name]
        cnt = f"  -- {row_counts[name]:,} 行" if row_counts and name in row_counts else ""
        blocks.append(f"### 表 {name}{cnt}\n-- 表注释: {t['comment']}")
        blocks.append(_annotate_ddl(name, t["columns"]) + ";")
        # 真实采样值（INSERT 注释形式）——注意不可执行，仅作格式参考
        if samples and name in samples and samples[name]:
            vals = ", ".join(repr(v) for v in samples[name][0])
            blocks.append(f"-- 采样行1: INSERT INTO {name} VALUES ({vals});")
            if len(samples[name]) > 1:
                vals2 = ", ".join(repr(v) for v in samples[name][1])
                blocks.append(f"-- 采样行2: INSERT INTO {name} VALUES ({vals2});")
        blocks.append("")
    blocks.append("### 表间关系（join 提示）")
    blocks += [f"-- {r}" for r in RELATIONS]
    return "\n".join(blocks).strip()


def render_fewshots_block(few_shots: list[dict]) -> str:
    """渲染 few-shot 示例块：question + SQL + reasoning（为什么这么写）。"""
    if not few_shots:
        return ""
    parts = ["### 历史相似问题示例（参考其写法）"]
    for i, fs in enumerate(few_shots, 1):
        parts.append(
            f"示例{i} 问题: {fs['question']}\n"
            f"参考SQL: {fs['sql']}\n"
            f"思路: {fs.get('reasoning', '')}"
        )
    return "\n".join(parts)


def build_system_prompt(
    *,
    schema_block: str,
    fewshots_block: str,
    dialect_note: str | None = None,
    profile: "ScenarioProfile | None" = None,
) -> str:
    """组装 System Prompt。fewshots_block 为空时自动省略该段（消融对照组）。

    业务口径（币种/默认状态/去重键）从场景包取值——换场景换 profile，
    规则措辞与数值不变。缺省用 Olist（当前唯一内置场景）。
    """
    from src.scenarios import get_profile

    p = profile or get_profile()
    dialect = dialect_note or p.dialect
    few = f"\n\n{fewshots_block}" if fewshots_block else ""
    return (
        f"你是{p.domain}业务数据分析助手。用户用自然语言提问，你需要把问题转成一条 SQL。\n"
        f"数据库方言: {dialect}（只读查询，禁止 INSERT/UPDATE/DELETE/DDL）\n\n"
        "要求:\n"
        "1. 只输出 SQL 本身，不要解释、不要 Markdown 代码块包裹、不要分号结尾外的多余内容；\n"
        f"2. 涉及金额单位一律保留原始币种 {p.currency}，不要换算；\n"
        "3. 时间比较用日期范围显式写出（如 purchase >= '2017-01-01' AND purchase < '2018-01-01'），"
        "不要用 strftime 模糊匹配；\n"
        f"4. 默认只统计已送达({p.default_status})订单，除非问题明确包含其他状态；\n"
        "5. 数值聚合保留两位小数；\n"
        f"6. 客户维度的去重/复购统计用 {p.dedup_key}（不是 customer_id）；\n"
        "7. 结果集加 LIMIT 1000 以内，防止打爆内存。\n\n"
        "以下是数据库 Schema（字段后的 -- 注释即字段含义）:\n\n"
        f"{schema_block}{few}\n\n"
        "请直接输出针对用户问题的 SQL:"
    )

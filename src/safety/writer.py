"""
写路径安全层（P2-S9：库的增删改查 —— 提案-人审 HITL）。

设计立场：
  - 读路径让 LLM 自主循环（四层安全兜底）；写路径**永不进自主循环**——
    LLM 只能「生成提案」，执行必须经过人类确认（HITL, Human-In-The-Loop）。
    这是 Agent 落地企业库时的标准姿势：模型有建议权，人有否决权与执行权。
  - 影响行数预估用「事务 dry-run」：BEGIN → 执行写语句 → SELECT changes() → ROLLBACK。
    零副作用拿到精确影响行数（不是 WHERE count 估算——同事务内语义完全一致），
    人看到「将影响 N 行」再决定，比裸 SQL 卡片可信得多。

三道闸（写语句校验 validate_write）：
  ① 语句类型白名单：仅单条 INSERT / UPDATE / DELETE
  ② 表名白名单：复用读路径的 sqlparse AST 表名提取（白名单 9 张业务表 + 动态注册表）
  ③ UPDATE/DELETE 必须带 WHERE（无 WHERE 的全表改写直接拒绝——highest risk 场景）
  外加：危险子串黑名单、多条语句拼接拒绝（与读路径同源实现）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import sqlparse

from src.safety.validator import (
    ALLOWED_TABLES,
    _stmt_real_count,
    _is_real_token,
    _BLOCKED_SUBSTR,
    extract_table_names,
    extract_cte_names,
)

# 写路径允许的语句类型（DDL/DCL 永远不允许 LLM 提案；建表走 CSV 导入的系统路径）
_WRITE_TYPES = {"INSERT", "UPDATE", "DELETE"}


@dataclass
class WriteCheckResult:
    passed: bool
    layer: str = ""        # stmt_type / table / where / dangerous
    reason: str = ""
    stmt_type: str = ""    # INSERT / UPDATE / DELETE
    tables: list[str] = field(default_factory=list)


def validate_write(sql: str) -> WriteCheckResult:
    """对 LLM 生成的写提案做三道闸校验（与读路径 validate_sql 同风格的 AST 判断）。"""
    if not sql or not sql.strip():
        return WriteCheckResult(False, layer="stmt_type", reason="SQL 为空")

    try:
        parsed = sqlparse.parse(sql)
    except Exception as e:
        return WriteCheckResult(False, layer="stmt_type", reason=f"SQL 解析失败: {e}")
    # 防拼接注入：与读路径同规则——真实语句数必须为 1
    if _stmt_real_count(parsed) != 1:
        return WriteCheckResult(False, layer="stmt_type", reason="仅允许单条语句（检测到拼接/多条）")

    stmt = next(p for p in parsed if any(_is_real_token(t) for t in p.tokens))
    stype = stmt.get_type()
    if stype not in _WRITE_TYPES:
        return WriteCheckResult(
            False, layer="stmt_type",
            reason=f"写提案仅允许 INSERT/UPDATE/DELETE（当前: {stype or 'UNKNOWN'}）")

    # 危险子串兜底（大小写不敏感原文扫描，与读路径共用黑名单）
    up = sql.upper()
    for bad in _BLOCKED_SUBSTR:
        if bad in up:
            return WriteCheckResult(False, layer="dangerous", reason=f"禁止危险子句: {bad}")

    # ② 表名白名单（INSERT INTO / UPDATE / DELETE FROM 的表位都被 AST 提取覆盖；
    #    CTE 别名同读路径逻辑排除——写语句极少带 WITH，但带上是对的）
    tables = extract_table_names(sql) - extract_cte_names(sql)
    bad_t = {t for t in tables if t not in ALLOWED_TABLES}
    if bad_t:
        return WriteCheckResult(
            False, layer="table", reason=f"越权写入表: {sorted(bad_t)}（白名单外）",
            tables=sorted(tables))

    # ③ UPDATE/DELETE 必须带 WHERE（AST 层判断；sqlparse 会把 WHERE 组成 Where
    #    分组节点，关键字不在语句顶层，必须 flatten 后找）
    if stype in ("UPDATE", "DELETE"):
        has_where = any(t.is_keyword and t.value.upper() == "WHERE"
                        for t in stmt.flatten())
        if not has_where:
            return WriteCheckResult(
                False, layer="where",
                reason="UPDATE/DELETE 必须带 WHERE 条件（拒绝全表改写）")

    return WriteCheckResult(True, stmt_type=stype, tables=sorted(tables))


# ---------------------------------------------------------------------------
# 执行层：dry-run 预估 + 正式执行（读写凭据分离）
# ---------------------------------------------------------------------------

def _rw_conn(db_path: str):
    """写凭据连接：**不带 mode=ro**（这就是「读写凭据分离」的 SQLite 形态；
    MySQL 形态是独立 agent_rw 账号）。autocommit 关闭，事务手动控制。"""
    import sqlite3
    con = sqlite3.connect(db_path, isolation_level=None)  # 手动事务
    con.row_factory = sqlite3.Row   # dry-run 取证样本要 dict(r)，tuple 转 dict 会炸
    return con


def dry_run_write(db_path: str, sql: str) -> tuple[bool, str, int, list[dict]]:
    """事务 dry-run：BEGIN → 写语句 → SELECT changes() 取精确影响行数 → ROLLBACK。

    返回 (成功?, 错误, 影响行数, 取证样本行)。
    取证样本：UPDATE/DELETE 先按其 WHERE 跑一条 SELECT * LIMIT 3——
    人审卡片上能看到「会被改到/删掉的行长什么样」，比只有数字直观。
    """
    con = _rw_conn(db_path)
    try:
        # 取证样本：把 UPDATE/DELETE 的 WHERE 部分拼成 SELECT 预览（仅展示用，失败不阻塞）
        sample: list[dict] = []
        s = sql.strip().rstrip(";")
        for kw in ("UPDATE", "DELETE FROM"):
            if s.upper().startswith(kw):
                where_pos = s.upper().rfind(" WHERE ")
                if where_pos > 0:
                    table = s[len(kw):].split("WHERE")[0].split("SET")[0].strip()
                    probe = f"SELECT * FROM {table} {s[where_pos+1:].strip()} LIMIT 3"
                    try:
                        sample = [dict(r) for r in con.execute(probe).fetchall()]
                    except Exception:
                        pass
                break
        con.execute("BEGIN")
        cur = con.execute(sql)
        affected = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 \
            else con.execute("SELECT changes()").fetchone()[0]
        con.execute("ROLLBACK")   # 零副作用回滚——这是 dry-run 的灵魂
        return True, "", affected, sample
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        return False, f"{type(e).__name__}: {str(e)[:150]}", 0, []
    finally:
        con.close()


def commit_write(db_path: str, sql: str) -> tuple[bool, str, int]:
    """正式执行：事务 + 提交，返回精确影响行数。调用方必须已经过 validate_write + 人审。"""
    con = _rw_conn(db_path)
    try:
        con.execute("BEGIN")
        cur = con.execute(sql)
        affected = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 \
            else con.execute("SELECT changes()").fetchone()[0]
        con.execute("COMMIT")
        return True, "", affected
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        return False, f"{type(e).__name__}: {str(e)[:150]}", 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 审计日志（data/db/write_audit.jsonl，一行一次决策——谁在何时批了什么）
# ---------------------------------------------------------------------------
def audit_log(db_path: str, event: dict) -> None:
    from pathlib import Path
    p = Path(db_path).parent / "write_audit.jsonl"
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

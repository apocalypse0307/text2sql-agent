"""
四层安全校验器（P2-S3）。

四层防护（SQLite 开发库的完整等价实现）：
  ① 只读账号   → src/db/connect.py 的 mode=ro URI（引擎层拒绝一切写操作）
  ② 表名白名单 → 本模块 AST 提取表名，必须 ⊆ 白名单（9 张业务表）
  ③ 行数限制   → 最外层无 LIMIT 自动注入 LIMIT 1000；执行时再按 1000 行截断兜底
                   （防大结果集打爆内存）
  ④ 语句超时   → SQLite progress_handler 每 1000 条虚拟指令回调，超 5000ms 抛错
                   （MySQL 8.4 用 SET MAX_EXECUTION_TIME=5000，语义等价）
外加语句类型白名单：只允许单条 SELECT——INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE
/CREATE/ATTACH/PRAGMA 一律拒绝。

关键实现原则：
  - 必须 sqlparse 解析 AST 判断，不能正则匹配关键字——注释里写 /* DELETE */ 或字符串
    里含 'drop' 会绕过正则。AST 的 flatten() 带 token 类型，能区分「注释/字符串里的词」
    与「真实关键字」。
  - 表名提取：定位 FROM/JOIN/UPDATE 关键字 token，取其后的 Identifier / IdentifierList /
    Parenthesis（子查询递归）——只拿「表位置」的标识符，避免把 SELECT 列的别名当表名。
  - 校验失败必须携带可读原因（layer + reason），respond 节点原样展示——「安全拦截要留证」。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import sqlparse
from sqlparse.sql import Function, Identifier, IdentifierList, Parenthesis
from sqlparse.tokens import Comment


def _is_real_token(t) -> bool:
    """非空白、非注释的真实 token（注释/字符串里的关键字不算语句）。"""
    if t.is_whitespace:
        return False
    tt = getattr(t, "ttype", None)
    if tt and tt in Comment:
        return False
    return True


def _stmt_real_count(parsed) -> int:
    """parse 结果中真实语句数（剔除纯空白/纯注释的解析产物）。"""
    n = 0
    for p in parsed:
        if any(_is_real_token(t) for t in p.tokens):
            n += 1
    return n

from src.db.schema import TABLES

# 白名单：9 张 Olist 业务表（+ S9 CSV 导入动态注册的 csv_* 表——见 db.schema.register_dynamic_table）
ALLOWED_TABLES: set[str] = set(TABLES.keys())

# 语句类型黑名单关键字（get_type 之外的 DDL/DML/DCL，一律拒绝）
_BLOCKED_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE",
    "REPLACE", "ATTACH", "DETACH", "VACUUM", "REINDEX", "PRAGMA",
    "GRANT", "REVOKE",
}
# 危险子句（数据外带/拖慢通道）——大小写不敏感原文扫描兜底
_BLOCKED_SUBSTR = ("INTO OUTFILE", "LOAD_FILE", "SLEEP(", "BENCHMARK(", "PG_SLEEP")

_MAX_ROWS = 1000          # ③ 行数上限
_TIMEOUT_MS = 5000        # ④ 超时（毫秒）
_INSTR_PER_CHECK = 1000   # progress_handler 每 1000 条虚拟指令检查一次


@dataclass
class ValidationResult:
    """校验结果。passed=False 时 layer+reason 供 respond 节点原样展示（留证）。"""
    passed: bool
    sql: str = ""                       # 校验后（已注入 LIMIT）的可执行 SQL
    layer: str = ""                     # 命中哪一层: stmt_type / table / limit
    reason: str = ""
    tables: list[str] = field(default_factory=list)   # 解析出的表名


def _from_identifier(tok, out: set[str]) -> None:
    """处理 FROM/JOIN 关键字后的表位 token：Identifier / IdentifierList / 子查询。"""
    if tok is None:
        return
    if isinstance(tok, IdentifierList):
        for item in tok.get_sublists():
            _from_identifier(item, out)
    elif isinstance(tok, Identifier):
        # 子查询 FROM (SELECT ...) t：str 以 ( 开头 → 递归内部
        s = str(tok)
        if s.lstrip().startswith("("):
            _scan_subqueries(tok, out)
            return
        # sqlparse 0.6.0: real_name 属性已移除，用 get_real_name()（去 alias 取真表名）
        name = (tok.get_real_name() or "").strip().strip('"`[]')
        if name:
            out.add(name.lower())
    elif isinstance(tok, Function):
        # 表名后跟列名括号会被 sqlparse 分组成 Function（如 INSERT INTO t(col) VALUES…），
        # 不接住它整个目标表就漏提取——写路径等于没有表白名单
        name = (tok.get_real_name() or "").strip().strip('"`[]')
        if name:
            out.add(name.lower())
    elif isinstance(tok, Parenthesis):
        _scan_subqueries(tok, out)


def _scan_subqueries(tok, out: set[str]) -> None:
    """递归一个 token 的所有 group 子树，找其中的 FROM/JOIN 关键字后接表位。
    覆盖：WHERE IN (SELECT ...)、子查询、JOIN 条件里的子查询等。"""
    if tok is None or not tok.is_group:
        return
    toks = list(tok.tokens)
    for i, sub in enumerate(toks):
        if sub.is_keyword and sub.value.upper() in ("FROM", "JOIN", "UPDATE", "INTO", "TABLE"):
            # 该关键字后第一个非空白的实质 token（skip whitespace / 下个关键字如 ON）
            for nxt in toks[i + 1:]:
                if nxt.is_whitespace or (nxt.is_keyword and nxt.value.upper() in ("ON", "USING", "AS")):
                    continue
                if nxt.is_keyword:
                    break
                _from_identifier(nxt, out)
                break
        elif sub.is_group:
            _scan_subqueries(sub, out)


def extract_table_names(sql: str) -> set[str]:
    """从 SQL 提取所有表名（AST 层，覆盖 join / 逗号多表 / 子查询递归）。"""
    out: set[str] = set()
    try:
        for stmt in sqlparse.parse(sql):
            _scan_subqueries(stmt, out)
    except Exception:
        pass
    return out


def extract_cte_names(sql: str) -> set[str]:
    """提取 WITH 定义的 CTE 别名（如 WITH t AS (...) SELECT * FROM t 里的 t）。

    为什么必须排除：CTE 名出现在 FROM/JOIN 的「表位」上，表名提取会把它当真表
    ——白名单校验就把合法 SQL 误杀了（S8 双跑评测 b3 实测踩中）。
    CTE 是查询内的临时命名结果集，不是库里的表，不该接受表白名单管辖。
    """
    out: set[str] = set()
    try:
        for stmt in sqlparse.parse(sql):
            _collect_cte(stmt, out)
    except Exception:
        pass
    return out


def _collect_cte(tok, out: set[str]) -> None:
    """递归找 WITH 关键字，收集其后的 CTE 定义名（覆盖子查询里的嵌套 WITH）。"""
    if tok is None or not getattr(tok, "is_group", False):
        return
    toks = list(tok.tokens)
    for i, t in enumerate(toks):
        if t.is_keyword and t.value.upper() == "WITH":
            # WITH 后连续的 Identifier / IdentifierList 都是 CTE 定义，直到出现其他关键字
            for nxt in toks[i + 1:]:
                if nxt.is_whitespace:
                    continue
                if nxt.is_keyword:
                    break
                nm = ""
                if isinstance(nxt, IdentifierList):
                    for item in nxt.get_sublists():
                        nm = (item.get_real_name() or "").strip().strip('"`[]').lower()
                        if nm:
                            out.add(nm)
                    continue
                if isinstance(nxt, Identifier):
                    nm = (nxt.get_real_name() or "").strip().strip('"`[]').lower()
                if nm:
                    out.add(nm)
                else:
                    break
        elif t.is_group:
            _collect_cte(t, out)



def validate_sql(sql: str) -> ValidationResult:
    """对 LLM 生成的 SQL 做四层安全校验（②表名白名单 + ③LIMIT 注入 + 语句类型白名单）。
    ①只读与 ④超时在执行层（execute_with_timeout / mode=ro URI）。
    """
    if not sql or not sql.strip():
        return ValidationResult(False, layer="stmt_type", reason="SQL 为空")

    # 防 ; DROP TABLE 拼接注入：parse 后真实语句数必须为 1
    try:
        parsed = sqlparse.parse(sql)
    except Exception as e:
        return ValidationResult(False, layer="stmt_type", reason=f"SQL 解析失败: {e}")
    if _stmt_real_count(parsed) != 1:
        return ValidationResult(False, layer="stmt_type", reason="仅允许单条语句（检测到拼接/多条）")

    stmt = next(p for p in parsed if any(_is_real_token(t) for t in p.tokens))
    if stmt.get_type() != "SELECT":
        return ValidationResult(
            False, layer="stmt_type",
            reason=f"仅允许 SELECT 查询（当前: {stmt.get_type() or 'UNKNOWN'}）",
        )

    # 语句类型黑名单：AST flatten 只命中真实关键字（注释/字符串里的词不算）
    for tok in stmt.flatten():
        if tok.is_keyword and tok.value.upper() in _BLOCKED_KEYWORDS:
            return ValidationResult(False, layer="stmt_type", reason=f"禁止语句类型: {tok.value.upper()}")
    up = sql.upper()
    for bad in _BLOCKED_SUBSTR:
        if bad in up:
            return ValidationResult(False, layer="stmt_type", reason=f"禁止危险子句: {bad}")

    # ② 表名白名单（WITH 定义的 CTE 别名是查询内临时结果集，不是库表，先排除）
    tables = extract_table_names(sql) - extract_cte_names(sql)
    bad = {t for t in tables if t not in ALLOWED_TABLES}
    if bad:
        return ValidationResult(
            False, layer="table",
            reason=f"越权访问表: {sorted(bad)}（白名单外，可用表: {len(ALLOWED_TABLES)} 张业务表）",
            tables=sorted(tables),
        )

    # ③ 无 LIMIT 自动注入 LIMIT 1000
    final_sql = _ensure_limit(sql)

    return ValidationResult(True, sql=final_sql, tables=sorted(tables))


def _has_limit_ast(sql: str) -> bool:
    """最外层是否已有 LIMIT——只看语句的顶层 token。

    不能 flatten() 全树找：子查询 / CTE 里的 LIMIT 管不住外层结果集，
    `... WHERE id IN (SELECT id FROM t LIMIT 5)` 外层照样能返回整张表。
    sqlparse 把括号里的内容分组成 Parenthesis，所以顶层出现的 LIMIT 就是外层的。
    """
    for tok in sqlparse.parse(sql)[0].tokens:
        if tok.is_keyword and tok.value.upper() == "LIMIT":
            return True
    return False


def _ensure_limit(sql: str) -> str:
    s = sql.strip().rstrip(";")
    if _has_limit_ast(s):
        return s + ";"
    # 换行再接 LIMIT：SQL 若以 `-- 注释` 结尾，同一行追加的 LIMIT 会被注释吞掉，这一层就白设了
    return f"{s}\nLIMIT {_MAX_ROWS};"


def execute_with_timeout(
    db_path: str,
    sql: str,
    *,
    timeout_ms: int = _TIMEOUT_MS,
    max_rows: int | None = _MAX_ROWS,
) -> tuple[bool, str, list[dict]]:
    """只读（mode=ro）+ 超时（progress_handler）执行，最多取 max_rows 行。
    返回 (成功?, 错误信息, rows)。

    max_rows 是第③层在执行侧的保险丝：validate_sql 只保证最外层「有」LIMIT，
    管不住模型自己写的 LIMIT 1000000——fetchmany 让超出的行根本不进内存。
    max_rows=None 表示不截断（只给需要看 SQL 自身 LIMIT 是否生效的测试用）。
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    start = time.time()

    def _progress():
        if (time.time() - start) * 1000 > timeout_ms:
            raise TimeoutError(f"查询执行超过 {timeout_ms}ms 已终止")

    conn.set_progress_handler(_progress, _INSTR_PER_CHECK)
    try:
        cur = conn.execute(sql)
        rows = cur.fetchall() if max_rows is None else cur.fetchmany(max_rows)
        return True, "", [dict(r) for r in rows]
    except Exception as e:
        # progress_handler 抛的异常经 sqlite3 C 层会变成 OperationalError: interrupted，
        # 翻译回超时语义（自愈节点需要知道是超时不是语法错）
        if "interrupted" in str(e).lower():
            return False, f"TimeoutError: 查询执行超过 {timeout_ms}ms 已终止", []
        return False, f"{type(e).__name__}: {str(e)[:150]}", []
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()

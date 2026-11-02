"""
CSV 导入核心（P2-S9）：文件字节 → 建表 → 批量插入 → 行数质检 → 动态注册。

职责边界：这是**系统路径**——DDL 由本模块受控生成，不经 LLM（LLM 路径的写
只能走 writer.validate_write 的 INSERT/UPDATE/DELETE 三道闸 + 人审）。
表名强制 csv_ 前缀 + 字符白名单清洗，列名同样清洗——外部文件名/表头不可信。
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field


@dataclass
class CsvImportResult:
    table: str
    columns: list[str]
    types: list[str]
    rows_in_file: int
    rows_inserted: int
    quality_ok: bool          # 文件数据行数 == 落库行数
    ddl: str = ""
    error: str = field(default="")


def _safe_table_name(stem: str) -> str | None:
    t = "csv_" + re.sub(r"[^0-9a-zA-Z_]", "_", stem).lower().strip("_")
    return t if re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", t) else None


def _safe_col_name(h: str, i: int) -> str:
    return re.sub(r"[^0-9a-zA-Z_]", "_", h.strip().lower()).strip("_") or f"col{i}"


def _col_type(vals: list[str]) -> str:
    """类型推断：采样前 50 行，全 int→INTEGER；含浮点→REAL；否则 TEXT。"""
    sample = [v.strip() for v in vals if v and v.strip()]
    if not sample:
        return "TEXT"

    def is_int(s):
        try:
            int(s); return True
        except ValueError:
            return False

    def is_float(s):
        try:
            float(s); return True
        except ValueError:
            return False

    if all(is_int(v) for v in sample):
        return "INTEGER"
    if all(is_float(v) for v in sample):
        return "REAL"
    return "TEXT"


def import_csv_bytes(filename: str, content: str, db_path: str) -> CsvImportResult:
    """导入一份 CSV（utf-8-sig 兼容 BOM；同名表覆盖）。失败即整体回滚。"""
    stem = re.sub(r"\.[cC][sS][vV]$", "", filename or "")
    table = _safe_table_name(stem)
    if table is None:
        return CsvImportResult(table="", columns=[], types=[], rows_in_file=0,
                               rows_inserted=0, quality_ok=False,
                               error=f"文件名无法转成合法表名：{filename}")

    rows = list(csv.reader(io.StringIO(content)))
    if len(rows) < 2:
        return CsvImportResult(table=table, columns=[], types=[], rows_in_file=0,
                               rows_inserted=0, quality_ok=False,
                               error="CSV 至少需要表头 + 1 行数据")
    header = [_safe_col_name(h, i) for i, h in enumerate(rows[0])]
    if len(set(header)) != len(header):
        return CsvImportResult(table=table, columns=header, types=[], rows_in_file=0,
                               rows_inserted=0, quality_ok=False,
                               error=f"表头存在重复列名：{header}")
    data = rows[1:]
    types = [_col_type([r[i] if i < len(r) else "" for r in data[:50]])
             for i in range(len(header))]
    ddl = f"CREATE TABLE {table} (\n" + ",\n".join(
        f"  {c} {t}" for c, t in zip(header, types)) + "\n)"

    con = sqlite3.connect(db_path)
    try:
        con.execute(f"DROP TABLE IF EXISTS {table}")   # 同名重传 = 覆盖
        con.execute(ddl)
        con.executemany(
            f"INSERT INTO {table} VALUES ({','.join('?' * len(header))})",
            [tuple((r[i] if i < len(r) else None) or None for i in range(len(header)))
             for r in data])
        inserted = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        con.commit()
    except Exception as e:
        con.rollback()
        return CsvImportResult(table=table, columns=header, types=types,
                               rows_in_file=len(data), rows_inserted=0,
                               quality_ok=False, error=f"{type(e).__name__}: {str(e)[:150]}")
    finally:
        con.close()

    # 注册进白名单 + Schema 注入（内存）+ 落盘（重启恢复）
    from src.db.schema import register_dynamic_table, save_custom_table_meta
    comment = f"CSV 导入表（{filename}，{inserted} 行）"
    register_dynamic_table(table, comment, ddl, {c: "CSV 导入列" for c in header})
    save_custom_table_meta(table, comment, ddl, {c: "CSV 导入列" for c in header},
                           db_path=db_path)

    return CsvImportResult(table=table, columns=header, types=types,
                           rows_in_file=len(data), rows_inserted=inserted,
                           quality_ok=len(data) == inserted, ddl=ddl)

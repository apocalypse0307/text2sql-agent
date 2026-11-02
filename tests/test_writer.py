"""
P2-S9 写路径测试：三道闸 / dry-run 零副作用 / commit+审计 / CSV 导入。

全部用临时 SQLite 库，不碰 Olist 主库、不调 LLM——安全层测试必须离线可跑。
运行：python -m pytest tests/test_writer.py -v
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.safety.writer import (
    validate_write,
    dry_run_write,
    commit_write,
    audit_log,
)
from src.db.csvimport import import_csv_bytes


# ---------------- fixtures ----------------

@pytest.fixture()
def tmp_db(tmp_path: Path) -> str:
    """带 customers/orders 两张白名单表的临时库（结构对齐 src/db/schema.py 子集）。"""
    p = tmp_path / "t.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY, city TEXT)")
    con.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, status TEXT)")
    con.executemany("INSERT INTO orders VALUES (?,?)",
                    [(f"o{i}", "delivered" if i < 3 else "canceled") for i in range(5)])
    con.commit()
    con.close()
    return str(p)


@pytest.fixture(autouse=True)
def _isolate_whitelist():
    """动态表注册会改全局白名单，测试后恢复快照（防测试间串扰）。"""
    from src.safety import validator
    from src.db.schema import TABLES
    snap_tables = set(validator.ALLOWED_TABLES)
    snap_schema = {k: dict(v) for k, v in TABLES.items() if k.startswith("csv_")}
    yield
    for name in list(validator.ALLOWED_TABLES):
        if name not in snap_tables:
            validator.ALLOWED_TABLES.discard(name)
    for name in snap_schema:
        TABLES.pop(name, None)


# ---------------- 三道闸 ----------------

class TestValidateWrite:
    def test_update_with_where_passes(self):
        r = validate_write('UPDATE orders SET status="canceled" WHERE order_id="o1"')
        assert r.passed and r.stmt_type == "UPDATE"

    def test_insert_passes(self):
        r = validate_write('INSERT INTO customers (customer_id, city) VALUES ("c9", "sao_paulo")')
        assert r.passed and r.stmt_type == "INSERT"

    def test_delete_without_where_rejected(self):
        r = validate_write("DELETE FROM orders")
        assert not r.passed and r.layer == "where"

    def test_update_without_where_rejected(self):
        r = validate_write("UPDATE orders SET status='x'")
        assert not r.passed and r.layer == "where"

    def test_ddl_rejected(self):
        for sql in ("DROP TABLE orders", "CREATE TABLE t(a INT)",
                    "ALTER TABLE orders ADD COLUMN x INT", "TRUNCATE TABLE orders"):
            r = validate_write(sql)
            assert not r.passed and r.layer == "stmt_type", sql

    def test_nonwhitelist_table_rejected(self):
        r = validate_write('INSERT INTO secret(pwd) VALUES ("1")')
        assert not r.passed and r.layer == "table"

    def test_multi_statement_rejected(self):
        r = validate_write('UPDATE orders SET status="x" WHERE order_id="o1"; DROP TABLE orders')
        assert not r.passed and r.layer == "stmt_type"

    def test_where_in_string_literal_not_fooled(self):
        # WHERE 藏在字符串里 ≠ 真有 WHERE（sqlparse AST 区分 token 类型）
        r = validate_write("DELETE FROM orders -- WHERE")
        assert not r.passed


# ---------------- dry-run：预估准确且零副作用 ----------------

class TestDryRun:
    def test_affected_count_accurate(self, tmp_db):
        ok, err, n, _ = dry_run_write(tmp_db, "UPDATE orders SET status='x' WHERE status='delivered'")
        assert ok and n == 3

    def test_zero_side_effect(self, tmp_db):
        con = sqlite3.connect(tmp_db)
        before = con.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall()
        con.close()
        ok, err, n, sample = dry_run_write(
            tmp_db, 'UPDATE orders SET status="canceled" WHERE order_id="o1"')
        con = sqlite3.connect(tmp_db)
        after = con.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall()
        row = con.execute("SELECT status FROM orders WHERE order_id='o1'").fetchone()[0]
        con.close()
        assert ok and n == 1 and sample, "dry-run 应返回取证样本行"
        assert before == after and row == "delivered", "dry-run 后数据必须原样"

    def test_syntax_error_reported(self, tmp_db):
        ok, err, n, _ = dry_run_write(tmp_db, "UPDAT orders SET x=1")
        assert not ok and "OperationalError" in err


# ---------------- commit + 审计 ----------------

class TestCommit:
    def test_commit_mutates(self, tmp_db):
        ok, err, n = commit_write(tmp_db, "UPDATE orders SET status='x' WHERE status='delivered'")
        con = sqlite3.connect(tmp_db)
        assert ok and n == 3
        assert con.execute("SELECT COUNT(*) FROM orders WHERE status='x'").fetchone()[0] == 3
        con.close()

    def test_audit_log_written(self, tmp_db, tmp_path):
        commit_write(tmp_db, 'UPDATE orders SET status="x" WHERE order_id="o1"')
        audit_log(tmp_db, {"event": "executed", "proposal_id": "p1", "affected": 1})
        audit_file = Path(tmp_db).parent / "write_audit.jsonl"
        assert audit_file.exists()
        ev = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert ev["event"] == "executed" and ev["proposal_id"] == "p1" and "ts" in ev


# ---------------- CSV 导入 ----------------

class TestCsvImport:
    CSV = "city,state,orders\nsao paulo,SP,120\nrio,RJ,80\ncuritiba,PR,\n"

    def test_import_roundtrip_and_queryable(self, tmp_db):
        r = import_csv_bytes("my report.csv", self.CSV, tmp_db)
        assert r.error == "" and r.quality_ok
        assert r.rows_in_file == 3 == r.rows_inserted
        assert r.table == "csv_my_report"
        assert r.types == ["TEXT", "TEXT", "INTEGER"]  # 空单元格不破坏整列类型推断
        # 动态表立即进白名单 → 写路径/读路径的 AST 白名单校验都能过
        from src.safety.validator import validate_sql
        assert validate_sql(f"SELECT SUM(orders) FROM {r.table}").passed
        assert validate_write(f'DELETE FROM {r.table} WHERE city="rio"').passed

    def test_duplicate_header_rejected(self, tmp_db):
        r = import_csv_bytes("dup.csv", "a,A\n1,2\n", tmp_db)
        assert r.error and "重复列名" in r.error

    def test_bad_filename_sanitized_or_rejected(self, tmp_db):
        # 乱码文件名：能清洗则转成 csv_ 前缀安全表名，清洗不下去（超长）必须拒绝
        r = import_csv_bytes("###.csv", "a\n1\n", tmp_db)
        assert r.error == "" and r.table == "csv_"  # ### → ___ → 剥掉尾部下划线
        r2 = import_csv_bytes("x" * 60 + ".csv", "a\n1\n", tmp_db)
        assert r2.error and r2.table == ""

    def test_reimport_overwrites(self, tmp_db):
        import_csv_bytes("over.csv", "a\n1\n", tmp_db)
        r = import_csv_bytes("over.csv", "a\n7\n8\n", tmp_db)
        # 断言返回值而不是丢掉：重复导入若失败，下面的行数断言会「碰巧」也失败，
        # 但那时报的是「行数不对」，真正的原因（导入被拒）被掩盖了。
        assert r.error == "", f"重复导入应成功覆盖，实际报错：{r.error}"
        con = sqlite3.connect(tmp_db)
        n = con.execute("SELECT COUNT(*) FROM csv_over").fetchone()[0]
        v = con.execute("SELECT a FROM csv_over ORDER BY a").fetchall()
        con.close()
        assert n == 2 and v == [(7,), (8,)]

    def test_registration_persisted(self, tmp_db):
        import_csv_bytes("persist.csv", "a\n1\n", tmp_db)
        reg = Path(tmp_db).parent / "custom_tables.json"
        metas = json.loads(reg.read_text(encoding="utf-8"))
        assert any(m["name"] == "csv_persist" for m in metas)

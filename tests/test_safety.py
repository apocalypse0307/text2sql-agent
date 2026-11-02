"""
四层安全校验器的 pytest 套件（安全拦截必须 100%——P2-S5 验收标准之一）。

覆盖：
  - 语句类型白名单：DELETE/UPDATE/INSERT/DROP 拒绝，注释/字符串内关键字不误杀
  - 表名白名单：直接越权 / sqlite_master / 子查询绕过 / 逗号多表
  - 拼接注入：; DROP TABLE 拒绝
  - LIMIT 注入：最外层无 LIMIT 自动补 LIMIT 1000；已有不重复；执行侧再按 1000 行截断
  - 只读：mode=ro 引擎层拒绝写
  - 超时：progress_handler 中断慢查询（百万行扫描）
运行：.venv/Scripts/python.exe -m pytest tests/test_safety.py -v
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from src.safety.validator import execute_with_timeout, validate_sql

DB = ROOT / "data" / "db" / "olist.db"

VALID = [
    ("SELECT COUNT(*) FROM orders"),
    ("SELECT c.customer_state, COUNT(*) FROM orders o JOIN customers c "
     "ON o.customer_id=c.customer_id WHERE o.order_status='delivered' GROUP BY 1"),
    ("SELECT * FROM orders /* DELETE FROM orders */"),          # 注释里藏关键字
    ("SELECT * FROM orders WHERE order_status='drop table'"),    # 字符串里含关键字
    ("select * from ORDERS"),                                   # 大小写
]


@pytest.mark.parametrize("sql", VALID)
def test_valid_select_passes(sql):
    r = validate_sql(sql)
    assert r.passed, r.reason
    assert r.sql.rstrip().endswith(";")


ATTACKS = [
    ("SELECT * FROM orders; DROP TABLE orders", "拼接注入"),
    ("DELETE FROM orders", "DELETE"),
    ("UPDATE orders SET order_status='x'", "UPDATE"),
    ("INSERT INTO orders VALUES ('x')", "INSERT"),
    ("SELECT * FROM (SELECT * FROM orders) t; DROP TABLE customers", "子查询后拼接"),
    ("SELECT * FROM users", "越权表"),
    ("SELECT name FROM sqlite_master", "系统表"),
    ("SELECT * FROM orders WHERE customer_id IN (SELECT customer_id FROM secret_table)", "子查询绕过"),
    ("SELECT * FROM orders, users", "逗号多表越权"),
    ("SELECT * FROM orders INTO OUTFILE /tmp/x", "数据外带"),
]


@pytest.mark.parametrize("sql,desc", ATTACKS)
def test_attack_blocked(sql, desc):
    r = validate_sql(sql)
    assert not r.passed, f"应拦截: {desc}"
    assert r.layer in ("stmt_type", "table"), r.layer


def test_limit_injected_once():
    r = validate_sql("SELECT * FROM orders")
    assert r.sql.count("LIMIT") == 1 and "LIMIT 1000" in r.sql
    r2 = validate_sql("SELECT * FROM orders LIMIT 5")
    assert r2.sql.count("LIMIT") == 1  # 已有 LIMIT 不重复注入


def test_readonly_blocks_write():
    ok, err, _ = execute_with_timeout(str(DB), "DELETE FROM orders")
    assert not ok and "readonly" in err.lower()


def test_timeout_interrupts_slow_query():
    # 慢必须慢在引擎里。原先用 `SELECT * FROM geolocation ORDER BY ...`，它在合成库上约 216ms，
    # 大头是把 40 万行全部拉进 Python；执行侧加了 1000 行截断后只要 ~60ms，
    # 超时根本触发不了（而且 216 vs 200 本来就是擦边过）。自连接计数是 O(n²) 的纯引擎计算、
    # 只返回一行，正是第④层该掐断的那类查询，与取多少行无关。
    ok, err, _ = execute_with_timeout(
        str(DB),
        "SELECT COUNT(*) AS n FROM geolocation a JOIN geolocation b"
        " ON a.geolocation_lat < b.geolocation_lat",
        timeout_ms=200,
    )
    assert not ok and "TimeoutError" in err and "200ms" in err


def test_normal_query_with_timeout_ok():
    ok, err, rows = execute_with_timeout(str(DB), "SELECT COUNT(*) AS c FROM orders")
    # 断言「>0 且有返回」而不是 == 99441：单元测试该钉**不变量**，不该钉**数据集基数**。
    # 写死真实行数会让这条用例只能在我这台建过真库的机器上跑——干净克隆里
    # data/db/olist.db 不存在（135MB，gitignore），CI 用的是 scripts/make_fixture_db.py
    # 生成的合成库（见该脚本 docstring）。真实基数由 init_db.py 的 EXPECTED 校验
    # 与 METRICS 负责，那才是该管它的地方。
    assert ok and rows and rows[0]["c"] > 0


# ---------------- S8 补：CTE 别名不得被表名白名单误杀 ----------------

class TestCteWhitelist:
    SQL_CTE = (
        'WITH max_month AS ('
        '  SELECT strftime("%Y-%m", order_purchase_timestamp) AS m, COUNT(*) AS n'
        '  FROM orders WHERE order_status=\'delivered\' GROUP BY m)'
        ' SELECT m, n FROM max_month ORDER BY n DESC LIMIT 1'
    )

    def test_cte_alias_not_treated_as_table(self):
        from src.safety.validator import validate_sql
        r = validate_sql(self.SQL_CTE)
        assert r.passed, f"CTE 别名被误杀: {r.reason}"

    def test_cte_does_not_mask_real_violation(self):
        from src.safety.validator import validate_sql
        sql = ('WITH t AS (SELECT * FROM orders)'
               ' SELECT * FROM t JOIN hackers ON 1=1')
        r = validate_sql(sql)
        assert not r.passed and 'hackers' in r.reason

    def test_cte_names_extraction(self):
        from src.safety.validator import extract_cte_names
        assert extract_cte_names(self.SQL_CTE) == {"max_month"}
        assert extract_cte_names("SELECT 1") == set()

    def test_cte_in_writer_path(self):
        # 写路径同样排除 CTE（理论上少用，但口径必须一致）
        from src.safety.writer import validate_write
        sql = ('WITH target AS (SELECT order_id FROM orders WHERE order_id=\'o1\')'
               ' UPDATE orders SET status=\'x\' WHERE order_id IN (SELECT order_id FROM target)')
        r = validate_write(sql)
        assert r.passed, f"CTE 写法被误杀: {r.reason}"


# ---------------- 第③层补：LIMIT 必须管住最外层（2026-10-26） ----------------

class TestOuterLimit:
    """修前 _has_limit_ast 用 flatten() 全树找 LIMIT：子查询 / CTE 里只要有一个 LIMIT，
    外层就不再注入；SQL 以 `-- 注释` 结尾时，同一行追加的 LIMIT 被注释吞掉；
    模型自己写 LIMIT 1000000 也照单全收。三种情况都会让第③层形同虚设。"""

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM orders WHERE order_id IN (SELECT order_id FROM order_items LIMIT 5)",
        "SELECT * FROM (SELECT * FROM orders LIMIT 5) t",
        "WITH t AS (SELECT * FROM orders LIMIT 5) SELECT * FROM t",
    ])
    def test_inner_limit_does_not_suppress_outer(self, sql):
        r = validate_sql(sql)
        assert r.passed
        assert r.sql.rstrip(";").endswith("LIMIT 1000"), r.sql

    @staticmethod
    def _orders_db(tmp_path, n: int) -> str:
        """只有 orders 一张表、n 行的临时库（表名要在白名单里，validate_sql 才放行）。"""
        import sqlite3
        p = tmp_path / "rows.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE orders (order_id TEXT)")
        con.executemany("INSERT INTO orders VALUES (?)", [(f"o{i}",) for i in range(n)])
        con.commit()
        con.close()
        return str(p)

    def test_trailing_line_comment_does_not_swallow_limit(self, tmp_path):
        db = self._orders_db(tmp_path, 1500)
        r = validate_sql("SELECT order_id FROM orders -- 全部订单")
        assert r.passed
        # max_rows=None：关掉执行侧截断，单看 SQL 里注入的 LIMIT 自己有没有生效
        ok, err, rows = execute_with_timeout(db, r.sql, max_rows=None)
        assert ok, err
        assert len(rows) == 1000

    def test_fetch_cap_bounds_explicit_huge_limit(self, tmp_path):
        db = self._orders_db(tmp_path, 1500)
        r = validate_sql("SELECT order_id FROM orders LIMIT 1000000")
        assert r.passed and "1000000" in r.sql       # 外层已有 LIMIT，SQL 不改写
        ok, err, rows = execute_with_timeout(db, r.sql)
        assert ok, err
        assert len(rows) == 1000                      # 执行侧保险丝兜住

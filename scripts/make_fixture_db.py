"""
生成「CI 用合成 fixture 库」（T1 CI 前置，2026-10-20）。

## 为什么需要它

`data/db/olist.db`（135 MB）由 `scripts/init_db.py` 从 Kaggle 的 Olist CSV 建出，
**数据集与库文件都不入库**（`.gitignore`：`data/raw/`、`data/db/*`、`*.db`）。
后果：干净克隆里没有库，凡是碰库的用例直接 `sqlite3.OperationalError`。

实测（干净克隆，无 `.env`/`models`/`data`）：

| 仓库 | 结果 |
|---|---|
| P1 | 64 passed —— 不碰库，CI 无需额外准备 |
| P2 | **137 passed / 10 failed / 2 skipped** —— 10 条全因缺库 |

所以 P2 的「默认 147 passed / 2 skipped，任何机器一致」这句话**当时并不成立**，
成立的是「任何**建过库**的机器」。这个脚本就是为了补上这个缺口。

## 为什么不下载真数据集

Kaggle 数据集需登录账号，CI 里要配 `KAGGLE_USERNAME`/`KAGGLE_KEY` 两个 secret，
且每次跑都拉 58 MB CSV 再花 20s 建 135 MB 库。对一个「验证代码路径」的 CI 来说
成本与收益不匹配。

## 为什么合成数据够用（逐条核对过失败用例的需求）

| 用例 | 对数据的真实需求 | 合成能否满足 |
|---|---|---|
| `test_legal_execute_passes_and_runs` | `orders` 表存在且 `COUNT(*) > 0` | ✅ |
| `test_readonly_blocks_write` | `orders` 表存在（验证被只读拦截，不关心行数） | ✅ |
| `test_timeout_interrupts_slow_query` | `geolocation` 足够大，`ORDER BY lat DESC` 超 200ms | ✅ 靠 `--geo-rows` 调够 |
| `test_mcp_select_roundtrip` | 同 `orders` | ✅ |
| `test_supervisor` 系列 | 断言里的 `45,090` 来自 **FakeLLM 的台词**，不是库里的数 | ✅ |
| `test_safety_block_yields_degraded` | 各表存在即可 | ✅ |

关键点：**没有一个用例依赖真实的业务数值**。`45,090` 那条容易误判——它是
`_FakeResp("2017 年已送达订单共 45,090 笔…")` 的固定输出，验的是「黑板字段能否
把 analysis 接住」，不是「库里有 45,090 笔订单」。

## 用法

```bash
python scripts/make_fixture_db.py                 # 写 data/db/olist.db，已存在则拒绝
python scripts/make_fixture_db.py --force         # 覆盖（**会毁掉真实库**，本地慎用）
python scripts/make_fixture_db.py --out /tmp/x.db --geo-rows 800000
```

CI 里就是第一条。**默认拒绝覆盖**是刻意的：本地真实库重建要 8.6s + 58MB CSV，
误跑一次的代价远大于省一个参数。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.db.schema import TABLES  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "db" / "olist.db"

# 除 geolocation 外每张表的合成行数：够让 COUNT(*)>0、JOIN 能跑通，又不拖慢 CI
ROWS_PER_TABLE = 200

# geolocation 默认行数：要让 `SELECT * FROM geolocation ORDER BY geolocation_lat DESC`
# 在 200ms 内跑不完（test_timeout_interrupts_slow_query 依赖这一点）。
# 40 万行实测能稳定超时；CI runner 通常比本机慢，只会更容易超时。
DEFAULT_GEO_ROWS = 400_000


def _columns(con: sqlite3.Connection, table: str) -> list[tuple[str, str, bool, int]]:
    """(列名, 类型, 是否 NOT NULL, 是否主键) —— 直接从建好的表反射，不手抄 schema。

    手抄 9 张表的列名会跟 `schema.py` 脱钩；DDL 是唯一真相源，这里只读它的结果。
    """
    return [(r[1], (r[2] or "").upper(), bool(r[3]), r[5])
            for r in con.execute(f"PRAGMA table_info({table})")]


def _make_value(typ: str, i: int, table: str, col: str, is_pk: bool):
    """按声明类型造一个确定性的占位值。

    确定性很要紧：同一个 `--seed` 下每次生成同样的库，CI 失败才可复现。
    """
    base = typ.split("(")[0]
    if is_pk or col.endswith("_id") or col == "customer_unique_id":
        # 主键/ID 列必须唯一，否则 PRIMARY KEY 约束直接炸
        return f"{table[:3]}_{i:08d}"
    if base in ("INTEGER", "INT", "BIGINT"):
        return i
    if base in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
        # 故意让值随 i 变化：ORDER BY / 排序类用例才有意义
        return round(1.0 + (i % 100000) * 0.0000137, 7)
    if base in ("DATETIME", "DATE", "TIMESTAMP"):
        # 统一落在 2017 年，与评测集里的年份口径一致
        return f"2017-{1 + i % 12:02d}-{1 + i % 28:02d} 0{i % 10}:00:00"
    # VARCHAR / TEXT / CHAR
    return f"v{i:08d}"


def build(out: Path, *, geo_rows: int = DEFAULT_GEO_ROWS,
          rows_per_table: int = ROWS_PER_TABLE) -> dict[str, int]:
    """建库并返回 {表名: 行数}。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    counts: dict[str, int] = {}
    con = sqlite3.connect(out)
    try:
        for table, spec in TABLES.items():
            con.execute(spec["ddl"])
            cols = _columns(con, table)
            n = geo_rows if table == "geolocation" else rows_per_table
            placeholders = ",".join("?" * len(cols))
            col_names = ",".join(c[0] for c in cols)
            sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
            batch = [
                tuple(_make_value(typ, i, table, name, pk) for name, typ, _nn, pk in cols)
                for i in range(n)
            ]
            con.executemany(sql, batch)
            counts[table] = n
        con.commit()
    finally:
        con.close()
    return counts


def main(*, out: Path = DEFAULT_OUT, force: bool = False,
         geo_rows: int = DEFAULT_GEO_ROWS) -> int:
    if out.exists() and not force:
        print(f"目标已存在，拒绝覆盖：{out}", file=sys.stderr)
        print("  这是 CI 专用合成库；本地真实库重建需 58MB CSV + 约 9s。", file=sys.stderr)
        print(f"  确实要覆盖就加 --force（会丢掉真实数据）", file=sys.stderr)
        return 3

    t0 = time.time()
    counts = build(out, geo_rows=geo_rows)
    size_mb = out.stat().st_size / 1e6
    print(f"已生成合成 fixture 库：{out}")
    print(f"  {len(counts)} 张表 / {sum(counts.values()):,} 行 / "
          f"{size_mb:.1f} MB / {time.time() - t0:.1f}s")
    for t, n in counts.items():
        print(f"    {t:<32} {n:>9,} 行")
    print("\n注意：这是**合成数据**，只保证 schema 与代码路径可跑，不含真实业务分布。")
    print("      真实数据请用 scripts/init_db.py（需 Kaggle 数据集）。")
    return 0


def _cli() -> None:
    ap = argparse.ArgumentParser(
        description="生成 CI 用的合成 Olist fixture 库（不需要 Kaggle 数据集）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"输出路径（默认 {DEFAULT_OUT}）")
    ap.add_argument("--force", action="store_true",
                    help="目标已存在时也覆盖（会毁掉真实库，本地慎用）")
    ap.add_argument("--geo-rows", type=int, default=DEFAULT_GEO_ROWS,
                    help=f"geolocation 行数（默认 {DEFAULT_GEO_ROWS}，用于让超时用例成立）")
    a = ap.parse_args()
    sys.exit(main(out=a.out, force=a.force, geo_rows=a.geo_rows))


if __name__ == "__main__":
    _cli()

"""
数据库初始化脚本 —— MySQL 版（P2 生产形态）。

与 scripts/init_db.py（SQLite dev 版）同源同校验：pandas.read_csv 读 Olist 9 表
→ to_sql 入 MySQL 8.4 的 olist 库；逐表行数与官方口径核对，对不上立即报错。
时间戳列统一转 DATETIME（比对/聚合与 SQLite TEXT 语义等价，且 MySQL 方言函数可用）。

语法：MYSQL_ROOT_PWD=<root密码> python scripts/init_db_mysql.py
      （root 只在本脚本内建表导数据用；运行时应用一律走 agent_ro 只读账号，
       见 src/db/connect.py 与 .env 的 DB_URL_MYSQL）
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

# 复用 SQLite 版的表名映射与期望行数，保证两库口径一致（单一数据源）
_spec = importlib.util.spec_from_file_location(
    "init_db_sqlite", ROOT / "scripts" / "init_db.py"
)
_init = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_init)
CSV_MAP: dict[str, str] = _init.CSV_MAP
EXPECTED: dict[str, int] = _init.EXPECTED

# 时间戳列 → DATETIME（其余列保持 pandas 推断，缺值一律 NULL）
DATE_COLS: dict[str, list[str]] = {
    "orders": [
        "order_purchase_timestamp", "order_approved_at",
        "order_delivered_carrier_date", "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ],
    "order_items": ["shipping_limit_date"],
    "order_reviews": ["review_creation_date", "review_answer_timestamp"],
}

HOST, PORT, DB = (
    os.environ.get("MYSQL_HOST", "127.0.0.1"),
    int(os.environ.get("MYSQL_PORT", "3306")),
    os.environ.get("MYSQL_DB", "olist"),
)
# 为什么这三项要能改：本机开发是 127.0.0.1，而 docker compose 里 MySQL 的
# 服务名是 `mysql`，容器内解析不到 127.0.0.1 上的库。硬编码会让 compose 形态
# 只能用「导数据前先手改代码」这种反模式。


def main() -> None:
    pwd = os.environ.get("MYSQL_ROOT_PWD", "")
    if not pwd:
        sys.exit("缺少 MYSQL_ROOT_PWD 环境变量（root 只用于本次导数据）")
    engine = create_engine(
        f"mysql+pymysql://root:{pwd}@{HOST}:{PORT}/{DB}?charset=utf8mb4",
        pool_pre_ping=True,
    )

    # 幂等：先清空 9 张表
    with engine.begin() as con:
        for csv_name, table in CSV_MAP.items():
            con.execute(text(f"DROP TABLE IF EXISTS `{table}`"))

    counts: dict[str, int] = {}
    t0 = time.time()
    for csv_name, table in CSV_MAP.items():
        df = pd.read_csv(RAW / csv_name)
        for col in DATE_COLS.get(table, []):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        df.to_sql(
            table, con=engine, if_exists="replace", index=False, chunksize=10000
        )
        with engine.connect() as con:
            n = con.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
        counts[table] = int(n)
        flag = "OK" if n == EXPECTED[table] else f"!= EXPECTED {EXPECTED[table]}"
        print(f"{table:28s} {n:>9,} rows  [{flag}]", flush=True)

    bad = {k: v for k, v in counts.items() if v != EXPECTED[k]}
    print("-" * 52)
    print(f"9 表共 {sum(counts.values()):,} 行，耗时 {time.time()-t0:.0f}s")
    if bad:
        sys.exit(f"行数校验失败: {bad}")
    print("行数全校验通过 [OK]")


if __name__ == "__main__":
    main()

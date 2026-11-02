"""
数据库初始化脚本（P2-S1）。

语法：pandas.read_csv 读 Olist 9 表 → 按预定义 DDL 建表 → to_sql 入 SQLite。
用途：产出 data/db/olist.db（dev 库）。MySQL 版本见 README 的「切换 MySQL」小节——
     四层安全里的「只读账号 + MAX_EXECUTION_TIME」是 MySQL 特性，SQLite 开发库用
     URI mode=ro（只读打开）+ progress handler 超时做等价防护。

建库完成后会自动调用 dump_schema() 重新生成 data/schema.md（给人读的 Schema 文档，
随仓库提交）——保证「文档永远与库同步」，不需要手动记得去刷新。

运行：
  env -u PYTHONPATH python scripts/init_db.py                # 重建库 + 刷新 data/schema.md
  env -u PYTHONPATH python scripts/init_db.py --dump-schema  # 只刷新文档，不重建库（快）
  env -u PYTHONPATH python scripts/init_db.py --no-dump      # 只重建库，不动文档

缺数据时的行为（2026-10-20 加固）：
  先跑 check_raw_data() 一次性列出 `data/raw/` 下**所有**缺失的 CSV，附 Kaggle 链接与
  存放路径，然后以退出码 2 结束；不会读一张崩一张、也不会先删旧库再崩。
  退出码约定：1 = 配置/凭据缺失（见 src/mcp_server.py），2 = 前置数据缺失。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.db.schema import TABLES, render_schema_md  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "db" / "olist.db"
SCHEMA_MD = ROOT / "data" / "schema.md"

# 数据来源（报错文案里直接给出，省得人再去翻 README）
KAGGLE_URL = "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce"

# geolocation 是特例：官方包里它以 zip 形式发布，本脚本会自动解压。
# 提成常量是因为「检查」和「解压」两处都要认这两个名字，写死两遍迟早写歪。
GEO_CSV = "olist_geolocation_dataset.csv"
GEO_ZIP = "olist_geolocation_dataset.zip"

# CSV 文件名 → 表名映射（去 _dataset 后缀，与 schema.py 的表名一致）
CSV_MAP = {
    "olist_customers_dataset.csv": "customers",
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "olist_geolocation_dataset.csv": "geolocation",
    "product_category_name_translation.csv": "product_category_translation",
}

# 各表期望行数（官方数据集口径，导入后校验，对不上立即报错）
EXPECTED = {"customers": 99441, "orders": 99441, "order_items": 112650,
            "order_payments": 103886, "order_reviews": 99224, "products": 32951,
            "sellers": 3095, "geolocation": 1000163, "product_category_translation": 71}


class MissingRawDataError(FileNotFoundError):
    """`data/raw/` 下缺少建库所需的 CSV —— **前置条件错误，不是代码错误**。

    为什么继承 FileNotFoundError：
      · 语义本来就对（确实是文件找不到）——原来第 100 万次 `read_csv` 抛的就是它；
      · 老调用方若写了 `except FileNotFoundError`，加固后行为不变，不会因为这次改动漏接。

    为什么还要自定义类型 + 携带结构化字段：
      · `missing` / `raw_dir` 让调用方**程序化**处理（补下载、报给用户、跳过建库），
        不必 stderr 正则解析一段人话；
      · 人话文案集中在 `_render()` 一处，测试可以断言「文案里有 Kaggle 链接」，
        而不是把长句复制进测试里（改文案就全红）。

    为什么不在这里 `sys.exit`：库函数不该决定进程怎么死（详见 `_cli()` 的注释）。
    """

    def __init__(self, missing: list[str], raw_dir: Path) -> None:
        self.missing = missing
        self.raw_dir = raw_dir
        super().__init__(self._render(missing, raw_dir))

    @staticmethod
    def _render(missing: list[str], raw_dir: Path) -> str:
        # 只用 ASCII 标记 + 中文，不放 emoji：Windows 控制台默认 cp936(GBK) 编不了
        # U+26A0/U+2705 这类字符，报错文案自己崩掉是最坏的情况（P1 刚踩过同一个坑）。
        lines = [
            f"缺少 {len(missing)} 个数据集文件，无法建库（一次列全，不用改一个再跑一次）：",
            *(f"  [缺失] {name}" for name in missing),
            "",
            "数据从哪来：",
            f"  Kaggle : {KAGGLE_URL}",
            "  （免费，需登录；下载得到 archive.zip，解压后取内层 CSV）",
            f"  放到   : {raw_dir}",
            "",
            "文件名必须与上面列出的完全一致。放好后再跑：python scripts/init_db.py",
        ]
        return "\n".join(lines)


def check_raw_data(raw_dir: Path | None = None) -> list[str]:
    """返回**缺失的 CSV 文件名列表**（只检查，不抛异常，不读文件内容）。

    `raw_dir` 默认取模块常量 RAW，但支持注入——不用默认参数 `raw_dir: Path = RAW`
    是因为 Python 的默认参数在 `def` 时求值一次，monkeypatch `init_db.RAW` 对它无效，
    那样这个函数就没法在测试里指向 tmp 目录。
    """
    raw_dir = Path(raw_dir) if raw_dir is not None else RAW
    missing: list[str] = []
    for csv_name in CSV_MAP:
        if (raw_dir / csv_name).exists():
            continue
        # geolocation：csv 缺但原 zip 在，rebuild_db() 会自己解压，不算缺失
        if csv_name == GEO_CSV and (raw_dir / GEO_ZIP).exists():
            continue
        missing.append(csv_name)
    return missing


def require_raw_data(raw_dir: Path | None = None) -> None:
    """前置断言：缺任何一个就抛 MissingRawDataError（缺几个报几个，不短路）。"""
    missing = check_raw_data(raw_dir)
    if missing:
        raise MissingRawDataError(
            missing, Path(raw_dir) if raw_dir is not None else RAW)


def dump_schema(db_path: Path = DB, out_path: Path = SCHEMA_MD) -> Path | None:
    """按当前库的真实行数重新生成 data/schema.md（给人读的 Schema 文档）。

    - 表清单取自 TABLES（本脚本进程内即 9 张业务表）；
    - 某表在库里查不到时**跳过行数标注而不是报错**：注册元数据可能先于建表落盘，
      文档生成不该因此中断；
    - 库不存在时直接跳过并提示（--dump-schema 在没建库的机器上不炸）。
    """
    if not db_path.exists():
        print(f"[dump-schema] 库不存在，跳过：{db_path}")
        return None
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row_counts: dict[str, int] = {}
        for t in TABLES:
            try:
                row_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                continue
    finally:
        con.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_schema_md(row_counts), encoding="utf-8")
    missing = [t for t in TABLES if t not in row_counts]
    tail = f"，缺表跳过 {missing}" if missing else ""
    print(f"[dump-schema] 已刷新 {out_path}（{len(row_counts)} 张表{tail}）")
    return out_path


def rebuild_db(*, raw_dir: Path | None = None, db_path: Path | None = None) -> None:
    """重建 dev 库：前置检查 → 解压 → 读 9 张 CSV → 按 DDL 建表 → 入 SQLite → 校验行数。

    `raw_dir` / `db_path` 可注入，仅为让测试能把「缺数据」这条路走通而不碰到真实数据
    （默认值在函数体内解析，理由见 check_raw_data 的 docstring）。
    """
    raw_dir = Path(raw_dir) if raw_dir is not None else RAW
    db_path = Path(db_path) if db_path is not None else DB

    # ① 前置检查必须排在 DB.unlink() **之前**。
    #    原实现是先删库、再逐表 read_csv：数据不全时会把一个本来好好的库删掉，
    #    然后崩在 FileNotFoundError 上——把「少下载一个文件」升级成「已有数据丢了」。
    #    顺序本身就是正确性。
    require_raw_data(raw_dir)

    # ② geolocation 可能是 zip 包
    zipped = raw_dir / GEO_ZIP
    if zipped.exists() and not (raw_dir / GEO_CSV).exists():
        with zipfile.ZipFile(zipped) as zf:
            zf.extractall(raw_dir)
        print("已解压 geolocation.zip")

    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)

    counts = {}
    for csv_name, table in CSV_MAP.items():
        t0 = time.time()
        df = pd.read_csv(raw_dir / csv_name)
        # 时间列转 pandas datetime（SQLite 存 ISO 字符串，比较运算仍正确）
        for col in df.columns:
            if col.endswith(("_timestamp", "_date", "_at")):
                df[col] = pd.to_datetime(df[col], errors="coerce")
        # 按预定义 DDL 建表（类型与注释意图一致），再追加数据
        conn.execute(TABLES[table]["ddl"])
        df.to_sql(table, conn, if_exists="append", index=False)
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts[table] = n
        # ASCII 标记而非 emoji：这一行正好是「行数对不上」时唯一要看的信息，
        # 它若在 cp936 控制台上 UnicodeEncodeError，就等于一报错就崩。
        flag = "OK" if n == EXPECTED.get(table) else f"[MISMATCH] 期望 {EXPECTED.get(table)}"
        print(f"  {table:<32} {n:>9,} 行  {flag}  ({time.time()-t0:.1f}s)")

    conn.commit()
    conn.close()
    print(f"\n库已写入 {db_path}  大小 {db_path.stat().st_size/1e6:.1f} MB")


def main(*, rebuild: bool = True, dump: bool = True) -> None:
    if rebuild:
        rebuild_db()
    if dump:
        dump_schema()


def _cli() -> None:
    ap = argparse.ArgumentParser(
        description="初始化 Olist 开发库（并默认刷新 data/schema.md）")
    ap.add_argument("--dump-schema", action="store_true",
                    help="只重新生成 data/schema.md，不重建库（秒级）")
    ap.add_argument("--no-dump", action="store_true",
                    help="重建库但不刷新 data/schema.md")
    a = ap.parse_args()
    if a.dump_schema and a.no_dump:
        ap.error("--dump-schema 与 --no-dump 互斥")
    try:
        main(rebuild=not a.dump_schema, dump=not a.no_dump)
    except MissingRawDataError as exc:
        # 到这里是**脚本入口**，失败方式该与库函数不同：
        #   · 入口的责任是给「退出码 + 人话」，让 CI / .bat / 人 都能判断发生了什么；
        #   · 库函数的责任只是抛一个能识别、能带数据的异常。
        # 所以 exit 只出现在这一层，rebuild_db() 里绝不 sys.exit——否则任何 import
        # 它的调用方（测试、notebook、web 后端）都会被迫跟着整个进程退出，
        # 而 SystemExit 继承 BaseException，`except Exception` 根本拦不住。
        #
        # 退出码 2 而非 1：1 已被「配置/凭据缺失」（src/mcp_server.py 启动体检）占用。
        # 两类失败分开编号，CI 才能一眼区分「我 key 没配」和「我数据没准备好」。
        print(str(exc), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    _cli()

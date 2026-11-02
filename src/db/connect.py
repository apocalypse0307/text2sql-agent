"""
数据库连接模块。

用途：所有 SQL 执行都走这里拿连接，禁止业务代码直接 sqlite3.connect。
原理：
  - db_url 抽象双引擎：开发期 SQLite（读=mode=ro 只读 URI）；生产切 MySQL 时
    db_url 换 mysql+pymysql://agent_ro:xxx@host/olist 即可，代码零改动
    ——「SQLAlchemy URL 抽象」正是「只读账号」这一层从 SQLite 到 MySQL
    无缝切换的落点。
  - 只读兜底随引擎变化但语义一致：SQLite 用 mode=ro URI（引擎层拒绝写）；
    MySQL 用 agent_ro 账号（DB 授权仅 SELECT，写操作 1142 denied）。
    两者都是"即使 SQL 注入绕过了解析层，数据库也拒绝写操作"。
"""
from __future__ import annotations

import sqlite3 as _sqlite3

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from src.core.config import settings


def get_conn(*, readonly: bool = True) -> Connection:
    """返回一个数据库连接。默认只读。

    SQLite：mode=ro 由引擎层面拒绝一切写操作（开发期等价实现）。
    MySQL ：连接串走 agent_ro 账号——只读由 DB 授权（仅 SELECT）保证，
            INSERT/UPDATE/DELETE 直接 1142 denied，与 mode=ro 同一安全语义。
    """
    if settings.db_url.startswith("mysql"):
        # MySQL 生产形态：应用只有 agent_ro（SELECT-only），无"写通道"可言
        engine = create_engine(settings.db_url, future=True)
    elif readonly:
        # SQLite 只读 URI：用 creator 直连 sqlite3，绕开 SQLAlchemy 对
        # "file:...?mode=ro" 的 URL 解析/转义，保证 mode=ro 原文送达引擎层
        engine = create_engine(
            "sqlite://",
            creator=lambda: _sqlite3.connect(settings.db_ro_uri, uri=True),
            future=True,
        )
    else:
        engine = create_engine(settings.db_url, future=True)
    return engine.connect()


def execute_select(sql: str, *, limit: int | None = 1000) -> list[dict]:
    """只读执行一条 SELECT，返回 list[dict]（列名 -> 值）。

    注意：limit 截断发生在取回全部行之后（内存峰值取决于 SQL 本身）——
    真正防大结果集的是 validator 第③层在 SQL 里注入 LIMIT，本参数只是保险丝。
    """
    conn = get_conn(readonly=True)
    try:
        rows = conn.execute(text(sql)).mappings().all()
        if limit is not None and len(rows) > limit:
            rows = rows[:limit]
        return [dict(r) for r in rows]
    finally:
        conn.close()

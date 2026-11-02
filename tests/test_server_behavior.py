"""
服务端两处行为回归（2026-10-26）：react 拦截提示 + 写提案确认的原子性。

  · _norm_react：validate_sql 节点拦截时写的是 status=blocked + reject_reason，
    原先只看 sql_error（该节点从不写它），被拦下的 SQL 在前端也显示「校验通过」。
  · /api/confirm：原先「先查 pending、再执行」两步之间没有锁，两个几乎同时的确认请求
    都能看到 pending，同一条写 SQL 被执行两次。改为带 status='pending' 条件的原子 UPDATE 抢占。

全离线：临时 service.db，审计与真正写库都换成桩，不碰真实库。
运行：python -m pytest tests/test_server_behavior.py -v
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import src.api.server as server  # noqa: E402


def test_react_blocked_validate_is_not_reported_as_passed():
    evs = server._norm_react("validate_sql", {
        "status": "blocked", "reject_reason": "[安全拦截·table] 越权访问表: ['users']"})
    assert len(evs) == 1
    assert "未过" in evs[0]["content"] and "users" in evs[0]["content"]
    passed = server._norm_react("validate_sql", {"sql": "SELECT 1 LIMIT 1000;"})
    assert "通过" in passed[0]["content"]


@pytest.fixture()
def proposal(tmp_path, monkeypatch):
    """临时 service.db 里放一条 pending 提案；审计与真正写库都换成桩。"""
    monkeypatch.setattr(server, "_SVC_DB", tmp_path / "service.db")
    server._svc_init()
    pid = "p_test_0001"
    with server._svc_conn() as con:
        con.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?)",
                    (pid, "把客户 c1 的城市改成 x",
                     "UPDATE customers SET customer_city='x' WHERE customer_id='c1'",
                     "UPDATE", '["customers"]', 1, "pending", server._utc(), None))
    monkeypatch.setattr(server, "audit_log", lambda *a, **k: None)
    commits: list[str] = []

    def _slow_commit(db_path, sql):
        commits.append(sql)
        time.sleep(0.3)               # 拉长「执行中」窗口，让并发请求一定撞在一起
        return True, "", 1

    monkeypatch.setattr(server, "commit_write", _slow_commit)
    return pid, commits


def test_claim_is_atomic(proposal):
    pid, _commits = proposal
    barrier = threading.Barrier(8)
    wins: list[bool] = []

    def _try():
        barrier.wait()
        wins.append(server._claim_proposal(pid, "executing"))

    threads = [threading.Thread(target=_try) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


def test_concurrent_confirms_execute_once(proposal):
    """双击「确认执行」：修前两个请求都看到 pending，commit_write 被调两次。"""
    pid, commits = proposal
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def _confirm():
        barrier.wait()
        try:
            outcomes.append(server.api_confirm(server.ConfirmRequest(proposal_id=pid)))
        except HTTPException as e:
            outcomes.append(e.status_code)

    threads = [threading.Thread(target=_confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(commits) == 1, f"同一提案被执行了 {len(commits)} 次"
    assert 409 in outcomes
    assert server._get_proposal(pid)["status"] == "executed"

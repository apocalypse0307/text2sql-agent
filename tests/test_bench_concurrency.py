"""
压测脚本的解析器自证（P2）。

## 为什么这一组用例比压测数字更重要

压测代码属于「计量器」。计量器错了，跑出来的每个数字都是错的，而且**看起来很正常**。
本文件钉的都是「错了也不报错、只会静默给出错误数字」的地方：

1. **分隔符是 `\\n\\n`**（`server.py:459` 裸 `StreamingResponse`），**不是** P1 的 `\\r\\n\\r\\n`。
2. **类型在 JSON 的 `type` 里**，不在 `event:` 行。
3. **跨包**：一帧可能被 TCP 切在分隔符中间。
4. **EOF 无 done**：`server.py:470-473` 的 except 不补 done。
   若按「流结束 = 完成」，error 请求会被记成「极快的成功」。

第 5 条 `test_p2_parser_rejects_p1_frames` 是**决定性断言**：只断言「能解析 P2 帧」是单边的，
喂 P1 的 `event:` + `\\r\\n\\r\\n` 帧进来必须解析不出 `done`。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_bench():
    """从路径加载 evals/bench_concurrency.py（evals 不是包，不能直接 import）。"""
    path = ROOT / "evals" / "bench_concurrency.py"
    spec = importlib.util.spec_from_file_location("bench_concurrency_p2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = _load_bench()


def _p2_stream(events: list[dict]) -> bytes:
    return b"".join(
        f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in events
    )


def _p1_stream(frames: list[tuple[str, dict]]) -> bytes:
    """P1 的真实帧形态：event: X + data: {...}，以 \\r\\n\\r\\n 分隔。"""
    out = b""
    for event, payload in frames:
        out += f"event: {event}\r\ndata: {json.dumps(payload, ensure_ascii=False)}\r\n\r\n".encode()
    return out


# ---------------------------------------------------------------- 帧解析
def test_parse_p2_frame_reads_data_only():
    frame = bench.parse_p2_frame(b'data: {"type": "done", "status": "ok"}')
    assert json.loads(frame["data"]) == {"type": "done", "status": "ok"}


def test_parse_p2_frame_ignores_event_line():
    """P2 没有 event 行；就算有也不该被当成类型来源（类型只在 JSON 的 type 里）。"""
    frame = bench.parse_p2_frame(b'event: done\ndata: {"type": "sql", "sql": "SELECT 1"}')
    assert json.loads(frame["data"])["type"] == "sql"


def test_parse_p2_frame_without_data_is_empty():
    assert bench.parse_p2_frame(b": keepalive")["data"] is None
    assert bench.parse_p2_frame(b"")["data"] is None


# ---------------------------------------------------------------- 流式解析
def test_parser_walks_full_stream_to_done():
    stream = _p2_stream([
        {"type": "thought", "step": "plan"},
        {"type": "sql", "step": "generate_sql", "sql": "SELECT COUNT(*) FROM orders"},
        {"type": "result", "rows": [], "total": 0},
        {"type": "answer", "step": "respond", "message": "共 0 笔", "status": "ok"},
        {"type": "done", "history_id": "h1", "status": "ok"},
    ])
    p = bench.P2SseParser()
    p.feed(stream)
    assert p.done == {"type": "done", "history_id": "h1", "status": "ok"}
    assert p.n_frames == 5
    assert p.n_thought == 1
    assert p.sqls == ["SELECT COUNT(*) FROM orders"]
    assert p.answer["message"] == "共 0 笔"
    ok, outcome, err = bench.classify_outcome(p)
    assert ok is True and outcome == "done" and err is None


def test_parser_keeps_last_sql_when_retried():
    """自愈会重试：`sql` 事件可能出现多次，取最后一条才是最终落地的 SQL。"""
    stream = _p2_stream([
        {"type": "sql", "sql": "SELECT bad FROM nowhere"},
        {"type": "sql", "sql": "SELECT COUNT(*) FROM orders"},
        {"type": "done", "status": "ok"},
    ])
    p = bench.P2SseParser()
    p.feed(stream)
    assert len(p.sqls) == 2
    assert p.sqls[-1] == "SELECT COUNT(*) FROM orders"


def test_parser_survives_frame_split_across_chunks():
    """TCP 不保证帧边界，逐字节喂最坏情况必须仍能拼回 done。"""
    stream = _p2_stream([
        {"type": "sql", "sql": "SELECT 1"},
        {"type": "done", "status": "degraded"},
    ])
    p = bench.P2SseParser()
    for i in range(len(stream)):
        p.feed(stream[i:i + 1])
    assert p.done is not None
    assert p.done["status"] == "degraded"
    assert p.sqls == ["SELECT 1"]


def test_parser_counts_bad_json_without_dying():
    p = bench.P2SseParser()
    p.feed(b"data: {not json\n\n")
    p.feed(b'data: {"type": "done"}\n\n')
    assert p.n_bad == 1
    assert p.done is not None


def test_p2_parser_rejects_p1_frames():
    """**决定性断言**：喂 P1 的 `\\r\\n\\r\\n` 帧，必须解析不出 `done`。

    只断言「能解析 P2 帧」是单边的——万一解析器碰巧两种都认，那条就白测了。
    两个项目的分隔符严格互斥（`\\r\\n\\r\\n` 里不含 `\\n\\n`），
    所以这里必须一刀切干净。
    """
    p1_bytes = _p1_stream([
        ("route", {"route": "cache"}),
        ("done", {"answer": "x", "cached": True}),
    ])
    p = bench.P2SseParser()
    p.feed(p1_bytes)
    assert p.done is None
    assert p.n_frames == 0           # 压根没切出帧来
    ok, outcome, _ = bench.classify_outcome(p)
    assert ok is False and outcome == "eof_no_done"


# ---------------------------------------------------------------- EOF 判定
def test_eof_without_done_is_not_success():
    """`server.py:470-473` 的 except 只塞 error 事件然后关流，不补 done。"""
    p = bench.P2SseParser()
    p.feed(_p2_stream([{"type": "thought", "step": "plan"},
                       {"type": "sql", "sql": "SELECT 1"}]))
    ok, outcome, err = bench.classify_outcome(p)
    assert ok is False
    assert outcome == "eof_no_done"
    assert "done" in err


def test_sse_error_frame_is_failure():
    p = bench.P2SseParser()
    p.feed(_p2_stream([{"type": "error", "step": "runtime",
                        "content": "OperationalError: database is locked"}]))
    ok, outcome, err = bench.classify_outcome(p)
    assert ok is False and outcome == "sse_error"
    assert "database is locked" in err


def test_error_takes_precedence_over_done():
    p = bench.P2SseParser()
    p.feed(_p2_stream([{"type": "error", "content": "boom"},
                       {"type": "done", "status": "ok"}]))
    ok, outcome, _ = bench.classify_outcome(p)
    assert ok is False and outcome == "sse_error"


# ---------------------------------------------------------------- 指标
def test_percentile_uses_nearest_rank():
    vals = [float(i) for i in range(1, 101)]
    assert bench.percentile(vals, 50) == 50
    assert bench.percentile(vals, 95) == 95
    assert bench.percentile(vals, 99) == 99
    assert bench.percentile([], 50) is None


def test_summarize_counts_failures():
    records = [
        {"ok": True, "wall_ms": 10.0, "outcome": "done"},
        {"ok": False, "wall_ms": 999.0, "outcome": "eof_no_done"},
    ]
    s = bench.summarize(records, wall_s=1.0)
    assert s["n"] == 2 and s["n_ok"] == 1
    assert s["qps"] == 1.0
    assert s["err_rate"] == 0.5
    # 失败请求的墙钟不能进分位数，否则 P99 会被超时值拉爆
    assert s["p50_ms"] == 10.0 and s["p99_ms"] == 10.0


# ---------------------------------------------------------------- 问题集
def test_goldset_excludes_security_items():
    """security 类按设计拒绝/净化，计入错误率会把「正常降级」误判成故障。"""
    items = bench.load_items()
    assert items, "goldset_s5 读不到"
    assert all(it["type"] != "security" for it in items)
    assert all(it.get("question") for it in items)
    raw = json.loads(bench.GOLDSET.read_text(encoding="utf-8"))
    assert len(items) == len(raw) - 6, "应排除恰好 6 条 security"


# ---------------------------------------------------------------- SQL 串场
def test_norm_sql_folds_whitespace_case_and_semicolon():
    assert bench._norm_sql("SELECT  COUNT(*)  FROM Orders ;") == "select count(*) from orders"
    assert bench._norm_sql("SELECT COUNT(*) FROM orders") == \
        bench._norm_sql("select count(*)\n  from orders;")


def test_sql_crosstalk_detects_foreign_gold():
    """决定性断言：拿 A 的题却产出 B 的金标 SQL，必须被记为串场。

    只断言「答对了几条」是单边的——串场时总数可能一模一样
    （都答对了，只是答的是别人的题）。必须按 id 交叉比对才算钉住。
    """
    items = [
        {"id": "a1", "gold_sql": "SELECT COUNT(*) FROM orders"},
        {"id": "b2", "gold_sql": "SELECT SUM(price) FROM items"},
    ]
    records = [
        {"qid": "a1", "sql": "SELECT COUNT(*) FROM orders"},     # 对的
        {"qid": "b2", "sql": "SELECT COUNT(*) FROM orders"},     # 串场：拿了 a1 的答案
    ]
    own, foreign = bench._sql_crosstalk(records, items)
    assert own == 1
    assert foreign == [("b2", "a1")]


def test_sql_crosstalk_zero_when_clean():
    items = [
        {"id": "a1", "gold_sql": "SELECT COUNT(*) FROM orders"},
        {"id": "b2", "gold_sql": "SELECT SUM(price) FROM items"},
    ]
    records = [
        {"qid": "a1", "sql": "SELECT COUNT(*) FROM orders"},
        {"qid": "b2", "sql": "SELECT SUM(price) FROM items"},
    ]
    own, foreign = bench._sql_crosstalk(records, items)
    assert own == 2 and foreign == []


def test_sql_crosstalk_ignores_missing_sql():
    """没拿到 SQL（degraded / blocked）不算串场，也不能算答对。"""
    items = [{"id": "a1", "gold_sql": "SELECT 1"}]
    own, foreign = bench._sql_crosstalk([{"qid": "a1", "sql": None}], items)
    assert own == 0 and foreign == []


# ---------------------------------------------------------------- 代理绕过
def test_client_kwargs_disable_env_proxy():
    """**压测必须绕开环境代理**——实测踩出来的，不是预防性洁癖。

    本机 shell 设了 `HTTP_PROXY=http://127.0.0.1:50297`（Clash）且没有 `NO_PROXY`。
    httpx 默认 `trust_env=True`，于是「压测本地服务」实际压的是代理：
    `/api/schema` 单发 22ms，10 并发 P50 直接变成 **13.5s**，
    而它只是序列化一个 8KB 的内存字典。第一版读数差点让人得出
    「FastAPI 框架层很慢」的结论——**归因错比没数据更糟**。
    """
    import httpx

    kw = bench.client_kwargs(conc=10, timeout_s=180.0)
    assert kw["trust_env"] is False, "缺 trust_env=False，请求会被环境代理接管"
    assert kw["follow_redirects"] is False

    cli = httpx.AsyncClient(**kw)
    try:
        assert cli._trust_env is False
    finally:
        import asyncio
        asyncio.run(cli.aclose())


def test_env_proxy_would_actually_hijack_requests(monkeypatch):
    """反向证明：开着 `trust_env` 时请求**确实**会被改道到代理。

    只断言「我们关掉了」是单边的——万一 httpx 哪天不再读 env，上一条就变成永真。
    实测（httpx 0.28.1）：代理挂在 transport 的 connection pool 上
    （`_pool._proxy_url`），不在 `_mounts` 的键名里（键是 URLPattern 对象）。
    直连时 pool 是普通 `ConnectionPool`，连 `_proxy_url` 属性都没有。
    """
    import httpx

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:59999")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    target = httpx.URL("http://127.0.0.1:501/whatever")
    hijacked = httpx.Client(trust_env=True)
    direct = httpx.Client(**bench.client_kwargs(conc=2, timeout_s=5.0))
    try:
        pool_proxy = getattr(hijacked._transport_for_url(target)._pool,
                             "_proxy_url", None)
        assert pool_proxy is not None, "trust_env=True 时请求应被环境代理接管"
        assert "59999" in str(pool_proxy)
        assert getattr(direct._transport_for_url(target)._pool,
                       "_proxy_url", None) is None
    finally:
        hijacked.close()
        direct.close()


# ---------------------------------------------------------------- 控制台编码
def test_markdown_row_is_console_encodable():
    """markdown 行要能打到 GBK 控制台。

    仓库有静态闸门（`test_output_encoding.py`），但它只看源码字面量；
    这条走**真实 f-string 求值结果**——若哪天表头加了 `\\u2248`/`\\u21d2`，
    静态闸门看不见，这条能抓。
    """
    s = {"qps": 12.3, "p50_ms": 10.0, "p95_ms": 20.0, "p99_ms": 30.0, "err_rate": 0.0}
    row = bench.md_row("P2-D 无 LLM", 300, 10, s, "无线程池争用迹象")
    row.encode("gbk")
    assert row.startswith("|") and row.endswith("|")


def test_cli_help_is_wellformed_and_needs_no_server():
    with pytest.raises(SystemExit) as ei:
        bench.build_parser().parse_args(["--help"])
    assert ei.value.code == 0
    with pytest.raises(SystemExit):
        bench.build_parser().parse_args(["--path", "nonexistent"])


# ============================================================================
# 严格口径落盘：让「0 串场」变成 artifact 可复现的结论
# ============================================================================
@pytest.fixture
def tiny_s5(tmp_path):
    """把**真实** `run_eval_s5` 的 DB 指向一个临时小库。

    刻意不自己做一套假比对：`_canon_rows()` 复刻了官方 `canon()` 的排序规则，
    两者分叉就会静默给出不同结论。所以这里用真的 `_norm_val` / `_cmp_key` /
    `compare_result`，只换库路径 —— 既能跑得飞快，又能交叉验证两者等价。

    必须挂快照 fixture：`_S5_CACHE` / `_SQL_CACHE` 都是模块级全局，
    污染会静默影响后面所有用例（本仓库为「全局状态被污染」单独踩过坑）。
    """
    mod = bench._load_s5()
    if mod is None:
        pytest.skip("run_eval_s5 不可用")

    db = tmp_path / "tiny.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (k TEXT, v REAL)")
    con.executemany("INSERT INTO t VALUES (?,?)",
                    [("a", 1.0), ("b", 2.0), ("c", 3.0)])
    con.commit()
    con.close()

    old_db = mod.DB
    saved_sql = dict(bench._SQL_CACHE)
    mod.DB = db
    bench._SQL_CACHE.clear()
    try:
        yield mod
    finally:
        mod.DB = old_db
        bench._SQL_CACHE.clear()
        bench._SQL_CACHE.update(saved_sql)


def _items(*ks):
    return [{"id": k, "gold_sql": f"SELECT v FROM t WHERE k = '{k}'"} for k in ks]


def _records(*ks):
    """候选 SQL 故意与金标文本不同：小写、多空格、带尾分号。

    真实 P2 里 `_norm_sql` 精确匹配命中自己 **0/22**，就是这种程度差异造成的。
    """
    return [{"qid": k, "sql": f"select   v  from t where k='{k}';"} for k in ks]


def test_canon_rows_agrees_with_official_compare_result(tiny_s5):
    """**交叉验证**：`_canon_sql` 的判定必须与官方 `compare_result` 完全一致。

    这是复刻 `canon()` 唯一的保险。列序/行序/浮点 round 任何一处走样，
    这里的 True/False 就会对不上。
    """
    pairs = [
        ("SELECT v FROM t WHERE k='a'", "select   v  from t where k='a';"),
        ("SELECT v FROM t WHERE k='a'", "SELECT v FROM t WHERE k='b'"),
        ("SELECT v FROM t WHERE k='a'", "SELECT sum(v) FROM t"),
        ("SELECT v FROM t",             "SELECT v FROM t ORDER BY v DESC"),
        ("SELECT v, k FROM t",          "SELECT k, v FROM t"),   # 列序不同
        ("SELECT round(v, 4) FROM t WHERE k='a'", "SELECT v FROM t WHERE k='a'"),
    ]
    for gold, cand in pairs:
        official, _ = tiny_s5.compare_result(gold, cand)
        g_state, g_val = bench._canon_sql(tiny_s5, gold)
        c_state, c_val = bench._canon_sql(tiny_s5, cand)
        mine = (g_state == "ok" and c_state == "ok" and g_val == c_val)
        assert mine == official, f"口径分叉：{gold!r} vs {cand!r}"


def test_strict_report_records_own_hits_despite_text_mismatch(tiny_s5):
    """文本级 0/3、执行级 3/3 —— 两个口径必须同时落盘，否则看不出差别。"""
    rep = bench.strict_report(_records("a", "b", "c"), _items("a", "b", "c"),
                             text_own=0)
    assert rep["text_own_hit"] == 0
    assert rep["strict_own_hit"] == 3
    assert rep["strict_scored"] == 3
    assert rep["strict_own_rate"] == 1.0
    assert rep["detector_power"] == "ok"
    assert rep["strict_foreign_count"] == 0
    assert rep["strict_xmatrix_skipped"] is None


def test_strict_report_blind_detector_is_marked_not_silently_zero(tiny_s5):
    """**决定性断言**：检测器失明时，`0 串场` 必须被判为「不可作结论」。

    只断言 `foreign == 0` 是单边的：一个永远认不出任何东西的检测器也能过。
    """
    blind = [{"qid": "a", "sql": "SELECT v FROM t WHERE k='zzz'"}]
    rep = bench.strict_report(blind, _items("a"), text_own=0)
    assert rep["strict_own_hit"] == 0
    assert rep["strict_foreign_count"] == 0          # 看起来「0 串场」
    assert rep["detector_power"] == "blind"          # 但结论是「测不出来」
    assert "不成立" in rep["detector_power_reason"]


def test_strict_report_unsupported_differs_from_blind(tiny_s5):
    """「没测」和「测了没问题」必须分开：装载失败要落成 unsupported。"""
    saved = dict(bench._S5_CACHE)
    bench._S5_CACHE.clear()
    bench._S5_CACHE.update({"mod": None, "err": "ModuleNotFoundError: 缺库"})
    try:
        rep = bench.strict_report(_records("a"), _items("a"), text_own=0)
    finally:
        bench._S5_CACHE.clear()
        bench._S5_CACHE.update(saved)
    assert rep["detector_power"] == "unsupported"
    assert "装载失败" in rep["detector_power_reason"]
    assert "矩阵未执行" in rep["strict_xmatrix_skipped"]
    assert [u["qid"] for u in rep["strict_unscored"]] == ["a"]


def test_strict_report_foreign_hit_is_saved_as_pair(tiny_s5):
    """串场要落成 (提问者, 被抄者) 二元组，只存个数查不出是哪两条。"""
    recs = [{"qid": "a", "sql": "SELECT v FROM t WHERE k='b'"}]   # a 拿到 b 的结果
    rep = bench.strict_report(recs, _items("a", "b"), text_own=0)
    assert rep["strict_own_hit"] == 0
    assert rep["strict_foreign_hits"] == [["a", "b"]]
    assert rep["strict_foreign_count"] == 1


def test_strict_report_records_scored_denominator(tiny_s5):
    """没 SQL 的记录不能算进分母，否则通过率会被稀释成假的好看。"""
    recs = _records("a") + [{"qid": "b", "sql": ""}]
    rep = bench.strict_report(recs, _items("a", "b"), text_own=0)
    assert rep["strict_own_hit"] == 1
    assert rep["strict_scored"] == 1                 # 不是 2
    assert rep["strict_own_rate"] == 1.0
    assert rep["strict_unscored"] == [{"qid": "b", "reason": "无 SQL"}]


def test_xmatrix_executes_each_distinct_sql_only_once(tiny_s5, monkeypatch):
    """**决定性断言**：交叉矩阵不能是 N^2 次 SQL 执行。

    原实现对每个 (候选, 他人金标) 组合都调一次 `compare_result`，
    于是同一条候选 SQL 被重复执行 N 次。实测这套 fixture 库 22 条金标自比对
    要 37.9 秒，乘上 22x21 就是十几分钟 —— 表现为「压测卡死」。
    按 SQL 文本缓存后，执行次数应为 2N（N 条金标 + N 条候选）。
    """
    calls = {"n": 0}
    real_connect = sqlite3.connect

    def counting(*a, **kw):
        calls["n"] += 1
        return real_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", counting)
    bench._SQL_CACHE.clear()
    hits, degenerate = bench.strict_crosstalk(
        _records("a", "b", "c"), _items("a", "b", "c"))
    assert hits == [] and degenerate == []
    # 3 金标 + 3 候选 = 6；若没缓存，会是 3 金标 x 3 候选 = 9 次
    assert calls["n"] == 6, f"执行了 {calls['n']} 次 SQL，缓存失效"


def test_canon_sql_times_out_instead_of_hanging(tiny_s5, monkeypatch):
    """病态 SQL 必须被超时打断，而不是把整轮压测挂死。"""
    monkeypatch.setattr(bench, "SQL_TIMEOUT_S", 0.2)
    state, val = bench._canon_sql(
        tiny_s5,
        "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM r"
        " WHERE i < 500000000) SELECT count(*) FROM r")
    assert state == "err"
    assert "interrupt" in str(val).lower()


def test_xmatrix_is_skipped_and_declared_when_too_large(tiny_s5, monkeypatch):
    """规模超限时跳过矩阵，但必须在 artifact 里写明跳过了。"""
    monkeypatch.setattr(bench, "XMATRIX_MAX", 1)
    rep = bench.strict_report(_records("a", "b"), _items("a", "b"), text_own=0)
    assert rep["strict_own_hit"] == 2                # 自比对仍然做了
    assert rep["strict_foreign_count"] == 0
    assert rep["strict_xmatrix_skipped"] is not None
    assert "XMATRIX_MAX" in rep["strict_xmatrix_skipped"]
    assert "矩阵未执行" in rep["conclusion"]


def test_strict_skipped_declares_no_evidence():
    """`--no-strict` 落盘的占位块不能长得像「验过了」。"""
    rep = bench._strict_skipped()
    assert rep["detector_power"] == "unsupported"
    assert rep["strict_own_hit"] is None
    assert "0 串场无严格证据" in rep["detector_power_reason"]
    assert rep["strict_xmatrix_skipped"]


def test_cli_strict_is_on_by_default_and_bounded():
    """严格口径默认开；且默认带上 SQL 超时与矩阵上限（否则慢查询会挂死压测）。"""
    a = bench.build_parser().parse_args(["--path", "crosstalk"])
    assert a.strict is True
    assert a.sql_timeout == bench.SQL_TIMEOUT_S and a.sql_timeout > 0
    assert a.xmatrix_max == bench.XMATRIX_MAX and a.xmatrix_max > 0
    assert bench.build_parser().parse_args(
        ["--path", "crosstalk", "--no-strict"]).strict is False


def test_degenerate_result_sets_are_not_counted_as_crosstalk(tiny_s5):
    """**决定性断言（来自真实误报）**：同答一个 0 不算串场。

    实测现场：`--path e` 22 条里交叉矩阵报出 4 处串场，全部是 f02 `(0,)`
    与 a06 `(0.0,)` / e01 `(0,)` / e03 `(0,)` / e04 `(0,)` 相等。
    但 f02 的 SQL 是对的——它问「2018 年 10 月已送达订单数」，fixture 里真是 0；
    那四题问的也是「有多少笔」，金标同样 0。**不同问题同答 0 是语义正确。**

    这里把该现场压成小库：`k='zzz'` 与 `k='none'` 两问的答案都是空集/0。
    """
    assert bench._is_degenerate([]) is True
    assert bench._is_degenerate([(0,)]) is True
    assert bench._is_degenerate([(0.0,)]) is True
    assert bench._is_degenerate([(None,)]) is True
    assert bench._is_degenerate([("",)]) is True
    assert bench._is_degenerate([(1,)]) is False
    assert bench._is_degenerate([(0, 5)]) is False       # 有一个非 0 就有信息
    assert bench._is_degenerate([("2024-01-01",)]) is False

    recs = [{"qid": "x", "sql": "SELECT COUNT(*) FROM t WHERE k='zzz'"}]
    items = [{"id": "x", "gold_sql": "SELECT COUNT(*) FROM t WHERE k='x'"},
             {"id": "y", "gold_sql": "SELECT COUNT(*) FROM t WHERE k='y'"}]
    rep = bench.strict_report(recs, items, text_own=0)
    assert rep["strict_foreign_count"] == 0              # 0 撞 0 不算串场
    # 矩阵只比「自己 vs 别人」，所以 x 只与 y 撞一次；但这一处必须留痕
    assert rep["strict_degenerate_matches"] == [["x", "y"]]
    assert "退化" in rep["conclusion"]


def test_real_valued_collision_is_still_reported(tiny_s5):
    """反向对照：值不退化时，真的同值必须照报——不能把过滤器做成开关。"""
    recs = [{"qid": "x", "sql": "SELECT COUNT(*) FROM t WHERE k='a'"}]
    items = [{"id": "x", "gold_sql": "SELECT COUNT(*) FROM t WHERE k='b'"},
             {"id": "y", "gold_sql": "SELECT COUNT(*) FROM t WHERE k='b'"}]
    rep = bench.strict_report(recs, items, text_own=0)
    assert rep["strict_foreign_hits"] == [["x", "y"]]
    assert rep["strict_degenerate_count"] == 0

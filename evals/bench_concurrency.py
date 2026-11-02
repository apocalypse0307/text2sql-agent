"""
并发压测骨架（P2 text2sql-agent）——按路径分解的性能表。

## 为什么不用 locust

`locust` 默认按**响应头到达**计时。对 SSE 来说响应头在第一个字节就到了，
流式请求会被记成个位数毫秒——比真实快两个数量级，是假数据。
本项目必须**消费 SSE 直到 `done`** 才算一次请求完成，所以自写
`httpx.AsyncClient` + `asyncio.Semaphore`。

## 两条路径（对应规格 §1）

| 路径 | 打什么 | 测什么 |
|---|---|---|
| `d` 无 LLM 接口 | `GET /api/schema` | 框架层（线程池 + 序列化）开销上限 |
| `e` 全链路取数 | `POST /api/query`（SSE） | LLM 串行往返 |

外加 `crosstalk`（并发串场）与 `ramp`（限流爬坡）。

## 三个必须写对的解析细节

1. **分隔符是 `\\n\\n`**。`server.py:459` 是
   `q.put(f"data: {json.dumps(ev)}\\n\\n")`，裸 `StreamingResponse`，**没有心跳**。
   这与 P1 完全不同（P1 的 `sse_starlette` 用 `\\r\\n\\r\\n`）——**两个项目各写一份解析器，
   不共用**。本文件的 `P2_S_SEP` 只认 `\\n\\n`，刻意不兼容 `\\r\\n\\r\\n`。
2. **类型在 JSON 里**，不在 `event:` 行。帧长这样：`data: {"type":"done",...}\\n\\n`。
3. **「流正常结束但没有 done」必须判失败**。`server.py:470-473` 的 `except`
   只塞一个 `type:"error"` 事件然后 `put(None)` 关流，**不补 done**。
   若按「流结束 = 完成」计时，error 请求会被记成「极快的成功」。

## `/api/schema` 的口径要说清

`api_schema()`（`server.py:497`）是 **`def`（同步）** 而不是 `async def`，
所以 FastAPI 把它丢进 anyio 线程池（默认 40 token）执行。
它测的是**线程池 + 序列化**，不是「事件循环层」。这个偏差要写进结论，
否则「框架没问题」这个断言就没有依据。

## 一个可以预测的结构性瓶颈（`ramp` 就是去验它）

每条 `/api/query` 占 **2 个线程**：
- 生产者：`threading.Thread(target=produce)`（`server.py:478`）
- 消费者：`await asyncio.to_thread(q.get)`（`server.py:480`）——
  **整条流的生命周期都占着 anyio 的 40-token 池**。

所以并发接近 20 时 `/api/query` 会先饿死自己，并且**连带拖慢 `/api/schema`（同池）**。
若实测成立，结论是「无限流时系统真实的失败模式不是 429，是静默排队到超时」——
比报一个 429 数字值钱得多。

## 语法

    # 0) 自检解析器（不联网、不花钱）
    .venv/Scripts/python.exe -m pytest tests/test_bench_concurrency.py -q

    # 1) 量测管线冒烟
    .venv/Scripts/python.exe evals/bench_concurrency.py --path d --dry-run

    # 2) D 路径（300 x 10）
    .venv/Scripts/python.exe evals/bench_concurrency.py --path d

    # 3) E 路径（20 x 2，走 agentic；约 1 毛钱）
    .venv/Scripts/python.exe evals/bench_concurrency.py --path e -n 20 -c 2

    # 4) 串场 + 爬坡
    .venv/Scripts/python.exe evals/bench_concurrency.py --path crosstalk --questions 5
    .venv/Scripts/python.exe evals/bench_concurrency.py --path ramp

问题来源 `evals/goldset_s5.json`（60 条），**自动排除 `type == "security"` 的 6 条**——
那 6 条按设计就是拒绝/净化，计入错误率会把「正常降级」误判成故障。

输出：终端 markdown 行（可直接贴进 METRICS）+ `evals/concurrency_<path>.json`。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDSET = ROOT / "evals" / "goldset_s5.json"

# 裸 StreamingResponse，帧以 \n\n 结尾。只认这个，不兼容 P1 的 \r\n\r\n。
P2_S_SEP = b"\n\n"


# ============================================================================
# SSE 解析
# ============================================================================
def parse_p2_frame(raw: bytes) -> dict:
    """把一帧拆成 {data, has_data}。P2 没有 `event:` 行，类型在 JSON 的 `type` 里。"""
    data_lines: list[str] = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep or field != "data":
            continue
        if value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
    return {"data": "\n".join(data_lines) if data_lines else None}


class P2SseParser:
    """增量解析 P2 的 SSE 流。状态只进不退，便于把「既无 done 也无 error」判成断流。"""

    def __init__(self) -> None:
        self._buf = b""
        self.n_frames = 0
        self.n_bad = 0
        self.n_thought = 0
        self.sqls: list[str] = []
        self.answer: dict | None = None
        self.done: dict | None = None
        self.error: str | None = None

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        while True:
            idx = self._buf.find(P2_S_SEP)
            if idx < 0:
                break
            raw, self._buf = self._buf[:idx], self._buf[idx + len(P2_S_SEP):]
            self._handle(parse_p2_frame(raw))

    def _handle(self, frame: dict) -> None:
        if not frame["data"]:
            self.n_bad += 1
            return
        try:
            ev = json.loads(frame["data"])
        except json.JSONDecodeError:
            self.n_bad += 1
            return
        self.n_frames += 1
        typ = ev.get("type")
        if typ == "sql" and ev.get("sql"):
            self.sqls.append(ev["sql"])
        elif typ == "thought":
            self.n_thought += 1
        elif typ == "answer":
            self.answer = ev
        elif typ == "done":
            self.done = ev
        elif typ == "error":
            self.error = ev.get("content") or ev.get("message") or "error"


def classify_outcome(parser: P2SseParser) -> tuple[bool, str, str | None]:
    """把解析器终态归成 (ok, outcome, error)。

    抽成纯函数是为了能被单测直接钉住：**「流结束但没 done」必须判失败**。
    这条写在 `chat_once` 里就只能起服务才测得到，而它恰恰最容易写错、
    写错了又不报错（错误请求会被记成「极快的成功」）。
    """
    if parser.error is not None:
        return False, "sse_error", parser.error
    if parser.done is None:
        return False, "eof_no_done", f"流结束但未收到 done（有效帧 {parser.n_frames}）"
    return True, "done", None


# ============================================================================
# 指标
# ============================================================================
def percentile(values: list[float], p: float) -> float | None:
    """最近秩法。样本少时比插值法保守，也不会有小数级幻读。"""
    if not values:
        return None
    s = sorted(values)
    k = int(math.ceil(p / 100.0 * len(s))) - 1
    return s[min(max(k, 0), len(s) - 1)]


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def summarize(records: list[dict], wall_s: float) -> dict:
    n = len(records)
    ok = [r for r in records if r["ok"]]
    wall = [r["wall_ms"] for r in ok]
    out = {
        "n": n, "n_ok": len(ok), "n_fail": n - len(ok),
        "qps": _r(len(ok) / wall_s, 3) if wall_s > 0 else None,
        "wall_s": _r(wall_s, 2),
        "p50_ms": _r(percentile(wall, 50)),
        "p95_ms": _r(percentile(wall, 95)),
        "p99_ms": _r(percentile(wall, 99)),
        "mean_ms": _r(sum(wall) / len(wall)) if wall else None,
        "err_rate": _r((n - len(ok)) / n, 4) if n else None,
        "outcomes": {},
    }
    for r in records:
        out["outcomes"][r["outcome"]] = out["outcomes"].get(r["outcome"], 0) + 1
    return out


def md_row(name: str, n: int, conc, s: dict, bottleneck: str) -> str:
    return ("| {name} | {n} | {c} | {qps} | {p50} | {p95} | {p99} | {err} | {bn} |").format(
        name=name, n=n, c=conc,
        qps=f"{s['qps']:.2f}" if s["qps"] is not None else "-",
        p50=f"{s['p50_ms']:.0f}ms" if s["p50_ms"] is not None else "-",
        p95=f"{s['p95_ms']:.0f}ms" if s["p95_ms"] is not None else "-",
        p99=f"{s['p99_ms']:.0f}ms" if s["p99_ms"] is not None else "-",
        err=f"{s['err_rate'] * 100:.1f}%" if s["err_rate"] is not None else "-",
        bn=bottleneck,
    )


# ============================================================================
# goldset
# ============================================================================
def load_items() -> list[dict]:
    data = json.loads(GOLDSET.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or []
    # security 类按设计就拒绝/净化，计入错误率会把「正常降级」误判成故障
    return [it for it in data if it.get("type") != "security"]


def _norm_sql(sql: str) -> str:
    """SQL 归一：小写 + 折叠空白 + 去尾分号。用于串场交叉比对，不判语义等价。

    刻意**不**做执行级比对：那是 `evals/run_eval_s5.compare_result` 的活，
    而串场检测只需要「这条响应的 SQL 更像哪道题的金标」这一个信号。
    """
    import re

    s = " ".join((sql or "").lower().split())
    # 先剥空白再剥分号，两步交替直到稳定：`"... orders ;"` 需要剥两轮
    # （只做一次 rstrip(";") 会留下 "orders " 的尾空格，导致归一化不等价）
    while s.endswith(";") or s.endswith(" "):
        s = s.rstrip()
        if s.endswith(";"):
            s = s[:-1]
    # validator 会给缺 LIMIT 的查询注入 `LIMIT n`（防大结果集），
    # 而金标里往往没写 —— 不剥掉会让「答对了」被判成「没命中」，
    # 串场检测的 own_hit 就会虚低，检测器失去区分力。
    s = re.sub(r"\s+limit\s+\d+$", "", s)
    return s


# ============================================================================
# 打流量
# ============================================================================
def client_kwargs(conc: int, timeout_s: float) -> dict:
    """构造 AsyncClient 的公共参数。

    ## `trust_env=False` 是必须的，不是洁癖

    本机 shell 里设了 `HTTP_PROXY=http://127.0.0.1:50297`（Clash）且**没有 NO_PROXY**。
    `httpx` 默认 `trust_env=True`，会把本地请求交给环境代理——于是「压测本地服务」
    实际压的是代理：`/api/schema` 单发 22ms，10 并发 P50 直接飙到 **13.5s**。
    第一版就是这个读数，差点让人得出「FastAPI 框架层很慢」的错误结论。

    压测本地服务时环境代理永远是噪声，必须显式关掉。
    这条也被 `tests/test_bench_concurrency.py::test_client_kwargs_disable_env_proxy` 钉住。
    """
    import httpx

    return {
        "timeout": httpx.Timeout(timeout_s, connect=10.0),
        "limits": httpx.Limits(max_connections=conc + 8,
                               max_keepalive_connections=conc + 8),
        "follow_redirects": False,
        "trust_env": False,
    }


async def schema_once(client, url: str) -> dict:
    t0 = time.perf_counter()
    rec = {"qid": "schema", "ok": False, "outcome": "unset",
           "http_status": None, "wall_ms": None, "n_tables": None,
           "bytes": None, "error": None}
    try:
        resp = await client.get(url)
        rec["http_status"] = resp.status_code
        payload_bytes = await resp.aread()
        rec["bytes"] = len(payload_bytes)
        if resp.status_code != 200:
            rec["outcome"] = "http_error"
            rec["error"] = f"HTTP {resp.status_code}: {payload_bytes[:200]!r}"
        else:
            payload = json.loads(payload_bytes.decode("utf-8"))
            rec["n_tables"] = len(payload.get("tables") or [])
            rec["ok"] = rec["n_tables"] > 0
            rec["outcome"] = "done" if rec["ok"] else "empty_schema"
    except Exception as e:                         # noqa: BLE001
        rec["outcome"] = "exception"
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["wall_ms"] = _r((time.perf_counter() - t0) * 1000.0)
    return rec


async def query_once(client, url: str, item: dict, thread_id: str, graph: str) -> dict:
    """发一条 /api/query，消费 SSE 直到 done / error / EOF。"""
    t0 = time.perf_counter()
    rec = {
        "qid": item.get("id"), "question": item["question"], "thread_id": thread_id,
        "ok": False, "outcome": "unset", "http_status": None,
        "wall_ms": None, "status": None, "sql": None, "n_frames": 0,
        "n_thought": 0, "answer_message": "", "error": None,
    }
    parser = P2SseParser()
    body = {"question": item["question"], "graph": graph, "thread_id": thread_id}
    try:
        async with client.stream("POST", url, json=body) as resp:
            rec["http_status"] = resp.status_code
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", "replace")[:300]
                rec["outcome"] = "http_error"
                rec["error"] = f"HTTP {resp.status_code}: {raw}"
            else:
                async for chunk in resp.aiter_raw():
                    parser.feed(chunk)
                rec["n_frames"] = parser.n_frames
                rec["n_thought"] = parser.n_thought
                ok, outcome, err = classify_outcome(parser)
                rec["ok"] = ok
                rec["outcome"] = outcome
                rec["error"] = err
                if ok:
                    rec["status"] = parser.done.get("status")
                    rec["sql"] = parser.sqls[-1] if parser.sqls else None
                    rec["answer_message"] = (parser.answer or {}).get("message", "")[:300]
    except Exception as e:                         # noqa: BLE001
        rec["outcome"] = "exception"
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["wall_ms"] = _r((time.perf_counter() - t0) * 1000.0)
    return rec


async def _run(url: str, items: list[dict], n: int, conc: int, timeout_s: float,
               kind: str, graph: str, progress: bool = True) -> tuple[list[dict], float]:
    """**闭循环 + 共享连接池**负载。

    ## 两个都是实测踩出来的，缺一个读数就废

    ### (1) 闭循环，而不是「n 个任务抢 conc 个信号量」
    第一版用信号量，且 `t0` 记在**抢信号量之前**，于是每条请求的 `wall_ms` 里
    混进了排队时间。`n=100, conc=10` 测出 P50 = 21.9s，而单发只要 22ms——
    因为最后一批的排队时间天然等于整轮墙钟，P50 必然 ≈ 总时长的一半。
    数字看起来完全正常（有值、错误率 0%、还能算 QPS），但它测的是
    「我的调度器排队多久」，不是「服务器多快」。
    闭循环下 worker 数就是并发数，`t0` 只覆盖请求本身，wall_ms 与服务耗时一致。

    ### (2) 共享一个 AsyncClient，而不是每条请求新建
    第一版每条请求 `async with httpx.AsyncClient(...)`，注释理由是
    「避免连接池上限变成隐式限流器」。理由听着对，做法错得离谱：
    实测 Windows 上**每个新建 client 的首次连接要 ~520ms 且互相串行化**。

    | 客户端模型（10 并发打 /api/schema） | 墙钟 |
    |---|---|
    | 独立 client x10 | 5836 ms（延迟 16/534/1108/.../5180 阶梯）|
    | 共享 client（连接池） | **24 ms** |

    差 240 倍。而同一时刻 curl 10 并发全是 2.5ms —— 服务端本来就没问题。
    **连接池上限的正确处理是把它调到 >= 并发数，不是绕开连接池。**
    """
    import httpx

    kwargs = client_kwargs(conc, timeout_s)
    cursor = 0
    records: list[dict] = []

    async def worker(cli) -> None:
        nonlocal cursor
        while True:
            i = cursor
            cursor += 1
            if i >= n:
                return
            item = items[i % len(items)] if items else {}
            tid = f"lt-{uuid.uuid4().hex}"
            if kind == "schema":
                rec = await schema_once(cli, url)
            else:
                rec = await query_once(cli, url, item, tid, graph)
            records.append(rec)
            if progress and len(records) % 20 == 0:
                print(f"  ... {len(records)}/{n}", flush=True)

    async with httpx.AsyncClient(**kwargs) as cli:
        t0 = time.perf_counter()
        await asyncio.gather(*[worker(cli) for _ in range(min(conc, n))])
        wall_s = time.perf_counter() - t0
    return records, wall_s


# ============================================================================
# 各路径
# ============================================================================
def _save(path_key: str, tag: str, payload: dict) -> None:
    out = ROOT / "evals" / f"concurrency_{path_key}{('_' + tag) if tag else ''}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out.relative_to(ROOT).as_posix()}")


def cmd_schema(a) -> int:
    """D 路径：无 LLM 基线。"""
    url = a.base_url.rstrip("/") + "/api/schema"
    n = 1 if a.dry_run else a.n
    print(f"[D 路径] GET /api/schema  {n} 条 @ 并发 {a.conc}")
    print("          注意：该端点是同步 def，跑在 anyio 线程池里（默认 40 token），")
    print("          所以它测的是「线程池 + 序列化」，不是事件循环。")
    records, wall_s = asyncio.run(_run(url, [], n, a.conc, a.timeout, "schema", "agentic"))
    s = summarize(records, wall_s)
    _print_summary(s, records)
    sizes = [r["bytes"] for r in records if r.get("bytes")]
    tables = {r["n_tables"] for r in records if r.get("n_tables")}
    if sizes:
        print(f"响应体积 P50 {percentile(sizes, 50):.0f} B，表数集合 {sorted(tables)}")
    print(f"D 路径  -> {md_row('P2-D 无 LLM', len(records), a.conc, s, _guess_d(s))}")
    if s["p50_ms"] is not None and s["p50_ms"] > 20:
        print("[WARN] P50 > 20ms：纯内存序列化不该这么慢，"
              "先排除 payload 体积（9 表全量 DDL）再怀疑框架")
    _save("D", a.tag, {"meta": _meta(a, url), "summary": s, "records": records})
    return 0 if s["n_fail"] == 0 else 1


def cmd_e2e(a) -> int:
    """E 路径：全链路取数。"""
    items = load_items()
    n = 1 if a.dry_run else min(a.n, len(items))
    url = a.base_url.rstrip("/") + "/api/query"
    print(f"[E 路径] POST /api/query  {n} 条 @ 并发 {a.conc}  graph={a.graph}")
    print(f"          问题取自 goldset_s5（已排除 security 类），可用 {len(items)} 条")
    records, wall_s = asyncio.run(
        _run(url, items, n, a.conc, a.timeout, "query", a.graph)
    )
    s = summarize(records, wall_s)
    _print_summary(s, records)
    statuses: dict[str, int] = {}
    for r in records:
        statuses[r.get("status") or "-"] = statuses.get(r.get("status") or "-", 0) + 1
    print(f"done.status 分布：{statuses}")
    if statuses.get("degraded"):
        print(f"[WARN] {statuses['degraded']} 条 degraded —— 要逐条解释原因，"
              f"它不等于「错误」，但也不等于「答对」")
    own, foreign = _sql_crosstalk(records, items)
    print(f"SQL 命中自己金标（归一化精确匹配）：{own}/{len(records)}")
    print(f"SQL 命中他人金标（串场）：{len(foreign)} 处"
          + (f"  -> {foreign[:3]}" if foreign else "  [OK] 0 串场"))
    _warn_if_weak(own, len(records))
    strict = strict_report(records, items, own) if a.strict else _strict_skipped()
    foreign = strict["strict_foreign_hits"] or foreign
    print(f"E 路径  -> {md_row('P2-E 全链路', len(records), a.conc, s, _guess_e(records))}")
    _save("E", a.tag, {"meta": _meta(a, url), "summary": s,
                       "statuses": statuses, "own_sql_hit": own,
                       "foreign_hits": foreign, "strict": strict,
                       "records": records})
    # 只有「严格口径自证有区分力且 0 串场」才算通过；
    # `unsupported` / `blind` 时 foreign 为空是**测不出来**，不能算通过。
    ok = (s["err_rate"] is not None and s["err_rate"] <= 0.05
          and not foreign and strict["detector_power"] == "ok")
    return 0 if ok else 1


def cmd_crosstalk(a) -> int:
    """并发正确性：N 个不同问题同时发，检查结果有没有串场。"""
    items = load_items()[: a.questions]
    url = a.base_url.rstrip("/") + "/api/query"
    print(f"[串场] {len(items)} 个不同问题 @ 并发 {len(items)}")
    records, wall_s = asyncio.run(
        _run(url, items, len(items), len(items), a.timeout, "query", a.graph)
    )
    s = summarize(records, wall_s)
    _print_summary(s, records)
    own, foreign = _sql_crosstalk(records, items)
    print(f"命中自己金标：{own}/{len(records)}")
    print(f"命中他人金标（串场）：{len(foreign)} 处"
          + (f"  -> {foreign[:5]}" if foreign else "  [OK] 0 串场"))
    _warn_if_weak(own, len(records))
    strict = strict_report(records, items, own) if a.strict else _strict_skipped()
    foreign = strict["strict_foreign_hits"] or foreign
    _save("crosstalk", a.tag, {"meta": _meta(a, url), "summary": s,
                               "own_sql_hit": own, "foreign_hits": foreign,
                               "strict": strict, "records": records})
    if foreign:
        return 3
    # 0 串场 + 检测器无区分力 = 没结论。退出码分开编（3=真串场，5=没测出来），
    # 注意 main() 里服务不可达也用 4，所以这里不能用 4，否则脚本化时无法区分。
    return 0 if strict["detector_power"] == "ok" else 5


def cmd_ramp(a) -> int:
    """限流爬坡：看从哪一档开始崩，以及**崩的形态是什么**。

    `/api/schema` 爬到 40 是利用零成本找线程池上限；
    `/api/query` 只爬两档，因为它一条就是一次完整 LLM 往返（约 5 厘）。
    """
    out_rows = []
    print()
    print("[爬坡 1/2] /api/schema（零成本，找线程池上限）")
    print("| 并发 | 请求 | QPS | P50 | P95 | P99 | 错误率 | 主要收尾 |")
    print("|---|---|---|---|---|---|---|---|")
    for conc in a.levels:
        recs, wall_s = asyncio.run(
            _run(a.base_url.rstrip("/") + "/api/schema", [], a.n, conc,
                 a.timeout, "schema", "agentic", progress=False)
        )
        s = summarize(recs, wall_s)
        top = max(s["outcomes"].items(), key=lambda kv: kv[1])
        out_rows.append({"kind": "schema", "concurrency": conc, "summary": s})
        print(f"| {conc} | {len(recs)} | {s['qps']:.2f} | {s['p50_ms']:.0f}ms | "
              f"{s['p95_ms']:.0f}ms | {s['p99_ms']:.0f}ms | "
              f"{s['err_rate'] * 100:.1f}% | {top[0]} |")

    items = load_items()
    print()
    print(f"[爬坡 2/2] /api/query（每档 {a.query_n} 条不同问题，"
          f"约 {a.query_n * len(a.query_levels) * 0.0054:.3f} 元）")
    print("| 并发 | 请求 | QPS | P50 | P95 | P99 | 错误率 | 主要收尾 |")
    print("|---|---|---|---|---|---|---|---|")
    for conc in a.query_levels:
        recs, wall_s = asyncio.run(
            _run(a.base_url.rstrip("/") + "/api/query", items, a.query_n, conc,
                 a.timeout, "query", a.graph, progress=False)
        )
        s = summarize(recs, wall_s)
        top = max(s["outcomes"].items(), key=lambda kv: kv[1])
        out_rows.append({"kind": "query", "concurrency": conc, "summary": s})
        print(f"| {conc} | {len(recs)} | {s['qps']:.2f} | {s['p50_ms']:.0f}ms | "
              f"{s['p95_ms']:.0f}ms | {s['p99_ms']:.0f}ms | "
              f"{s['err_rate'] * 100:.1f}% | {top[0]} |")
    print()
    print("判读要点：若 query 档位的慢，同时 schema 档位的 P99 也同向变差，")
    print("说明两者在抢同一个 anyio 线程池（40 token），而不是上游 429。")
    _save("ramp", a.tag, {"meta": _meta(a, None), "rows": out_rows})
    return 0


# ============================================================================
# 辅助
# ============================================================================
# 单条 SQL 的执行上限（秒）。候选 SQL 来自模型，可能写出笛卡尔积；
# 没有这个上限时，一条病态 SQL 就能把整轮压测挂死。
SQL_TIMEOUT_S = 8.0
# 交叉矩阵的规模上限：矩阵本身是内存比较，但 2N 条 SQL 要真执行。
# 超过这个 N 就跳过矩阵，并在 artifact 里写明「跳过了」，不伪装成「没问题」。
XMATRIX_MAX = 24

# `evals/run_eval_s5` 模块的惰性加载缓存：{"mod": module|None, "err": str|None}
_S5_CACHE: dict = {}
# SQL 文本 -> ("ok", 规范化结果集) | ("err", 错误串)。见 `_canon_sql()` 的说明。
_SQL_CACHE: dict = {}


def _canon_rows(rows, norm_val, cmp_key) -> list:
    """把结果集规范化成可比较的形态：行内排序 + 行间排序，浮点 round 2。

    这段与 `run_eval_s5.compare_result()` 里的 `canon()` 必须逐字一致，
    否则两个口径会悄悄分叉。因此它不自己发明规则，而是**向 s5 模块借用**
    `_norm_val` / `_cmp_key`；`tests/test_bench_concurrency.py` 里有一条
    交叉验证用例，拿真实 `compare_result()` 对同一批 SQL 断言结论相同。
    """
    inner = [tuple(sorted(r, key=cmp_key)) for r in rows]
    return sorted(inner, key=lambda row: tuple(cmp_key(v) for v in row))


def _sql_crosstalk(records: list[dict], items: list[dict]) -> tuple[int, list]:
    """交叉比对：这条响应的 SQL 是否命中**自己**的金标、是否命中**别人**的金标。

    命中别人的金标 = 串场（阻断级）。命中自己的 = 答对。
    两者都没有 = 答得不对但没串场，不算串场缺陷。
    """
    gold = {it["id"]: _norm_sql(it.get("gold_sql", "")) for it in items}
    own = 0
    foreign: list[tuple[str, str]] = []
    for rec in records:
        cand = _norm_sql(rec.get("sql") or "")
        if not cand:
            continue
        if gold.get(rec["qid"]) == cand:
            own += 1
        for other_id, other_gold in gold.items():
            if other_id == rec["qid"] or not other_gold:
                continue
            if other_gold == cand:
                foreign.append((rec["qid"], other_id))
    return own, foreign


def strict_report(records: list[dict], items: list[dict], text_own: int) -> dict:
    """执行级严格校验 —— **结构化落盘**，让 artifact 自己证明结论。

    ## 为什么必须落盘

    「0 串场」只有在**检测器有区分力**时才是结论。区分力 = 检测器能把这条响应
    认回它自己的金标。此前 JSON 里只存了**文本级** `own_sql_hit`（实测 0/20），
    于是从 artifact 完全看不出严格口径下命中了自己几次 ——
    「0 串场」既不可复现，也无法被反驳。这里把两个口径并排落盘：

    | 键 | 含义 |
    |---|---|
    | `text_own_hit` | 归一化 SQL 文本精确命中自己金标（弱检测器） |
    | `strict_own_hit` | 执行结果集命中自己金标（强检测器） |
    | `strict_foreign_hits` | 执行结果集命中**他人**金标（串场，阻断级） |

    判据（写进 `detector_power`）：

    - `ok`：`strict_own_hit > 0` → 强检测器看得见，`0 串场` 可信；
    - `blind`：装载成功但 `strict_own_hit == 0` → 连自己都认不出，
      `0 串场` **不可作为结论**；
    - `unsupported`：`compare_result` 没装起来（缺 fixture 库等）→
      这一轮**没有**严格证据，不能拿 `0 串场` 说事。

    最后一条是这次补落盘时最容易被忽略的：把「没测」和「测了没问题」
    混成一个 0，正是审计时最容易被打回的地方。
    """
    _strict_check(records, items)                 # 写 rec["strict_ok"] / ["strict_detail"]
    scored = [r for r in records if "strict_ok" in r]
    own = sum(1 for r in scored if r["strict_ok"])
    supported = _load_s5() is not None

    unscored = [
        {"qid": r.get("qid"),
         "reason": "无 SQL" if not r.get("sql") else "无金标"}
        for r in records if "strict_ok" not in r
    ]
    if not supported:
        power = "unsupported"
        why = f"compare_result 装载失败（{_S5_CACHE.get('err')}），本轮无严格证据"
    elif own == 0:
        power = "blind"
        why = "执行级比对连自己的金标都命中不了，串场结论不成立"
    else:
        power = "ok"
        why = "执行级比对能认回自己的金标，具备区分力"

    # 交叉矩阵：规模超限就**明说跳过**。绝不能让「没做」在 JSON 里长得像「没问题」。
    if not supported:
        foreign, degenerate, x_skip = [], [], "compare_result 不可用，矩阵未执行"
    elif len(records) > XMATRIX_MAX:
        foreign, degenerate, x_skip = [], [], (
            f"N={len(records)} > XMATRIX_MAX={XMATRIX_MAX}，"
            f"交叉矩阵未执行（0 串场仅覆盖自己→自己）")
    else:
        (foreign, degenerate), x_skip = strict_crosstalk(records, items), None
    report = {
        "text_own_hit": text_own,
        "text_own_rate": _r(text_own / len(records)) if records else None,
        "text_detector_has_power": bool(text_own),
        "strict_own_hit": own,
        "strict_scored": len(scored),
        "strict_own_rate": _r(own / len(scored)) if scored else None,
        "strict_foreign_hits": [list(p) for p in foreign],
        "strict_foreign_count": len(foreign),
        # 「看起来像串场但值退化为 0/NULL/空集」的匹配：不计入串场，但留痕。
        # 见 `_is_degenerate()`：不同问题同答 0 是语义正确，不是串场。
        "strict_degenerate_matches": [list(p) for p in degenerate],
        "strict_degenerate_count": len(degenerate),
        "strict_unscored": unscored,
        "strict_xmatrix_skipped": x_skip,
        "detector_power": power,
        "detector_power_reason": why,
        "conclusion": (
            f"严格口径：命中自己 {own}/{len(scored)}，命中他人 {len(foreign)} 处"
            + (f"（另有 {len(degenerate)} 处同值匹配因结果集退化被排除）"
               if degenerate else "")
            + (f"（矩阵未执行：{x_skip}）" if x_skip else "")
            + f"；检测器 {power}（{why}）"
        ),
    }
    print(f"[严格] 结果集语义比对通过（命中自己金标）：{own}/{len(scored)}")
    if x_skip:
        print(f"[严格] 交叉矩阵跳过：{x_skip}")
    else:
        print(f"[严格] 结果集交叉矩阵命中他人金标（串场）：{len(foreign)} 处"
              + (f"  -> {foreign[:5]}" if foreign else "  [OK] 0 串场"))
        if degenerate:
            print(f"[严格] 另有 {len(degenerate)} 处同值匹配因结果集退化"
                  f"（0/NULL/空集）被排除，不计为串场：{degenerate[:5]}")
    print(f"[严格] 区分力：{power} —— {why}")
    return report


def _load_s5():
    """惰性加载 `evals/run_eval_s5` 模块（执行级比对的家）。

    返回**模块**而不只是 `compare_result`：执行级比对需要一套归一化函数
    （`_norm_val` / `_cmp_key` / `DB`），借用的必须与官方评测器**同一份**，
    否则两处口径会分叉而没人发现。

    缓存同时让 `strict_report` 能区分「检测器瞎了」（`blind`）和
    「检测器根本没装上」（`unsupported`）——这两件事的结论完全不同。
    """
    if "mod" in _S5_CACHE:
        return _S5_CACHE["mod"]
    mod, err = None, None
    try:
        spec = importlib.util.spec_from_file_location(
            "run_eval_s5", ROOT / "evals" / "run_eval_s5.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:                        # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        print(f"[WARN] compare_result 不可用：{err}")
    _S5_CACHE["mod"], _S5_CACHE["err"] = mod, err
    return mod


def _is_degenerate(canon) -> bool:
    """结果集是否退化到「不能当指纹」：空集，或只有 0 / NULL / 空串。

    ## 为什么必须单独判

    实测（`--path e`，22 条）：交叉矩阵报出 4 处「串场」，全部来自 f02
    `(0,)` 与 a06 `(0.0,)` / e01 `(0,)` / e03 `(0,)` / e04 `(0,)` 相等。
    但 f02 的 SQL 是**对的**（`strict_ok=True`），它问的「2018 年 10 月已送达
    订单数」在这份 fixture 里真就是 0；而那四题问的也是「有多少笔」，
    金标同样是 0。

    **不同问题答出同一个 0 是语义正确，不是串场。** 一个退化值不携带任何
    区分信息，拿它当指纹必然误报——这跟「0/20 的文本检测器没有区分力」
    是同一类错误，只是发生在**值**这一层而不是检测器层。

    所以：退化匹配不计入串场，但也不丢弃，单独落盘供人复核。
    """
    if not canon:
        return True
    for row in canon:
        for v in row:
            if isinstance(v, str):
                if v.strip() != "":
                    return False
            elif v not in (0, 0.0, None):
                return False
    return True


def _canon_sql(mod, sql: str) -> tuple[str, object]:
    """执行一条 SQL 并返回 `("ok", 规范化结果集)` 或 `("err", 错误串)`，**按 SQL 文本缓存**。

    ## 为什么要缓存：不缓存就是 O(N^2) 次昂贵执行

    原实现直接调 `compare_result(gold_j, cand_i)` 做交叉矩阵，于是**同一条候选 SQL
    会被重复执行 N 次**。实测这套 fixture 库的金标本身就不便宜——22 条 gold 自比对
    要 37.9 秒（m07 6.9s / m02 6.2s / m08 5.4s），乘上 22×21 的矩阵就是十几分钟，
    看起来像「卡死」，其实是二次方成本撞上了慢查询。

    按 SQL 文本缓存之后，**每条不同的 SQL 只执行一次**：N 条金标 + N 条候选 = 2N 次，
    矩阵比对退化成内存里的集合比较。

    ## 为什么要有超时

    候选 SQL 来自模型，可能写出笛卡尔积。`sqlite3` 的 progress handler 让它在
    `sql_timeout` 秒时中断并返回错误，而不是把整轮压测挂死。
    """
    if sql in _SQL_CACHE:
        return _SQL_CACHE[sql]
    import sqlite3

    deadline = time.monotonic() + SQL_TIMEOUT_S
    try:
        con = sqlite3.connect(f"file:{mod.DB}?mode=ro", uri=True)
    except Exception as e:                        # noqa: BLE001
        res: tuple[str, object] = ("err", f"DB_ERR {type(e).__name__}: {str(e)[:80]}")
        _SQL_CACHE[sql] = res
        return res
    try:
        # 每 20000 条 vm 指令查一次墙钟；返回非 0 即中断
        con.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, 20000)
        rows = con.execute(sql).fetchall()
        res = ("ok", _canon_rows(rows, mod._norm_val, mod._cmp_key))
    except Exception as e:                        # noqa: BLE001
        res = ("err", f"{type(e).__name__}: {str(e)[:80]}")
    finally:
        con.close()
    _SQL_CACHE[sql] = res
    return res


def strict_crosstalk(records: list[dict],
                     items: list[dict]) -> tuple[list[tuple[str, str]],
                                                 list[tuple[str, str]]]:
    """执行级交叉矩阵：存在 i != j 使得 record_i 的 SQL 结果集 == gold_j 的结果集。

    ## 为什么归一化 SQL 比对对 P2 没用

    实测：20 条 P2-E 里 `_norm_sql` 精确命中自己金标 **0/20**，
    而执行级结果集比对通过 **18/20**。说明模型写出来的 SQL 文本与金标差得远
    （别名、字段顺序、括号），但结果集一致。
    在 0/20 的检测器上，「没有命中他人金标」几乎恒成立——
    那不是「没有串场」，是**检测器看不见**。用结果集比对才有区分力。

    代价：每轮要执行 2N 条**不同**的 SQL（金标 + 候选，各只执行一次），
    再在内存里做 N^2 比较。这套 fixture 库的单条金标 SQL 就不便宜
    （22 条里 m07 6.9s、m02 6.2s），所以 N 仍是有限制的：
    `--xmatrix-max` 之外的 N 会跳过矩阵并在 artifact 里写明跳过了，
    **不允许「没做」和「做了没问题」在 JSON 里长得一样**。
    返回 `(真串场, 因结果集退化而被排除的匹配)`。第二项不是噪声——
    它把「看起来像串场但值不携带信息」的情况显式留痕，便于人工复核。
    """
    mod = _load_s5()
    if mod is None:
        return [], []
    gold = {it["id"]: it.get("gold_sql", "") for it in items}
    # 先各算一次规范化结果集（2N 次执行，且被 `_canon_sql` 缓存），
    # 再在内存里做 N^2 比较 —— 而不是执行 N^2 次 SQL。
    gold_canon = {k: _canon_sql(mod, v) for k, v in gold.items() if v}
    hits: list[tuple[str, str]] = []
    degenerate: list[tuple[str, str]] = []
    for rec in records:
        cand = rec.get("sql")
        if not cand:
            continue
        c_state, c_val = _canon_sql(mod, cand)
        if c_state != "ok":
            continue
        for other_id, (g_state, g_val) in gold_canon.items():
            if other_id == rec["qid"] or g_state != "ok":
                continue
            if c_val != g_val:
                continue
            # 双方任一退化（0 / NULL / 空集）就不算证据
            if _is_degenerate(c_val) or _is_degenerate(g_val):
                degenerate.append((rec["qid"], other_id))
            else:
                hits.append((rec["qid"], other_id))
    return hits, degenerate


def _strict_skipped() -> dict:
    """`--no-strict` 时落盘的占位结构。

    故意写成 `detector_power="unsupported"` 而不是省略整个键：
    只要 artifact 里出现过「0 串场」，就必须能查到当时用的是哪个口径，
    否则下次有人翻到这份 JSON 会以为严格口径也验过了。
    """
    return {
        "text_own_hit": None, "text_own_rate": None,
        "text_detector_has_power": None,
        "strict_own_hit": None, "strict_scored": 0, "strict_own_rate": None,
        "strict_foreign_hits": [], "strict_foreign_count": 0,
        "strict_degenerate_matches": [], "strict_degenerate_count": 0,
        "strict_unscored": [], "strict_xmatrix_skipped": "本轮未做严格口径复核",
        "detector_power": "unsupported",
        "detector_power_reason": "--no-strict：本轮未做执行级复核，0 串场无严格证据",
        "conclusion": "本轮未做严格口径复核（--no-strict）",
    }


def _warn_if_weak(own: int, n: int) -> None:
    """检测器区分力自检：own_hit 太低时，「0 串场」没有说服力。

    串场检测的逻辑是「这条响应的 SQL 命中了别人的金标」。如果它连自己的金标都
    命中不了（归一化精确匹配本来就严），那么「没命中别人的」也很容易成立——
    这不是「没有串场」，而是「检测器看不见」。**把测不准和测得准分开报。**
    """
    if n and own / n < 0.3:
        print(f"[WARN] 区分力不足：仅 {own}/{n} 命中自己金标，"
              f"此时「0 串场」不可作为结论。以 --strict（执行级结果集比对，默认开）为准。")


def _strict_check(records: list[dict], items: list[dict]) -> None:
    """执行级比对（复用 `evals/run_eval_s5.compare_result`）。

    故意做成可选：它要连 fixture 库、每条执行两次 SQL，是「最终报告」用的强断言，
    不适合塞进快速骨架自检。导入失败只告警，不打断——压测不该被评测器拖死。
    """
    mod = _load_s5()
    if mod is None:
        print("[WARN] compare_result 不可用，跳过严格比对")
        return
    gold = {it["id"]: it.get("gold_sql", "") for it in items}
    for rec in records:
        g, c = gold.get(rec["qid"]), rec.get("sql")
        if not g or not c:
            continue
        g_state, g_val = _canon_sql(mod, g)
        if g_state != "ok":
            rec["strict_ok"] = False
            rec["strict_detail"] = f"GOLD_ERR {g_val}"
            continue
        c_state, c_val = _canon_sql(mod, c)
        if c_state != "ok":
            rec["strict_ok"] = False
            rec["strict_detail"] = f"EXEC_ERR {c_val}"
            continue
        rec["strict_ok"] = bool(c_val == g_val)
        rec["strict_detail"] = "" if rec["strict_ok"] else "结果集不一致"


def _guess_d(s: dict) -> str:
    if s["p50_ms"] is None:
        return "全失败"
    if s["p95_ms"] and s["p50_ms"] and s["p95_ms"] > 5 * s["p50_ms"]:
        return "线程池排队"
    return "无线程池争用迹象"


def _guess_e(records: list[dict]) -> str:
    p50 = percentile([r["wall_ms"] for r in records if r["ok"]], 50)
    if p50 is None:
        return "全失败"
    return "LLM 串行往返" if p50 > 5000 else "待定"


def _meta(a, url) -> dict:
    return {
        "path": getattr(a, "path", None),
        "n": getattr(a, "n", None),
        "concurrency": getattr(a, "conc", None),
        "graph": getattr(a, "graph", None),
        "timeout_s": getattr(a, "timeout", None),
        "url": url,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": getattr(a, "tag", ""),
    }


def _print_summary(s: dict, records: list[dict]) -> None:
    print()
    print(f"完成 {s['n_ok']}/{s['n']}  QPS {s['qps']}  "
          f"P50 {s['p50_ms']}ms  P95 {s['p95_ms']}ms  P99 {s['p99_ms']}ms  "
          f"错误率 {s['err_rate'] * 100:.1f}%")
    print(f"收尾分布：{s['outcomes']}")
    for r in [x for x in records if not x["ok"]][:5]:
        print(f"  [FAIL] {r.get('qid')} {r['outcome']}: {r['error']}")


# ============================================================================
# CLI
# ============================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="P2 并发压测（SSE 消费到 done 才计时）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--path", required=True, choices=["d", "e", "crosstalk", "ramp"],
                   help="d=无 LLM 接口 e=全链路 crosstalk=串场 ramp=爬坡")
    p.add_argument("--base-url", default="http://127.0.0.1:8501")
    p.add_argument("-n", "--n", type=int, default=0)
    p.add_argument("-c", "--concurrency", type=int, default=0, dest="conc")
    p.add_argument("--questions", type=int, default=5, help="crosstalk 用几条不同问题")
    p.add_argument("--graph", default="agentic", choices=["agentic", "react", "supervisor"])
    p.add_argument("--levels", type=int, nargs="+", default=[2, 5, 10, 20, 40],
                   help="ramp 里 /api/schema 的并发档位")
    p.add_argument("--query-levels", type=int, nargs="+", default=[2, 5],
                   help="ramp 里 /api/query 的并发档位（每条一次 LLM，慎加）")
    p.add_argument("--query-n", type=int, default=5, help="ramp 里每档 query 条数")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--no-strict", dest="strict", action="store_false",
                   help="关掉执行级复核（默认开：0 串场必须有区分力自证才作数）")
    p.add_argument("--strict", action="store_true", default=True,
                   help="用 compare_result 做执行级结果集比对（要连 fixture 库）")
    p.add_argument("--sql-timeout", type=float, default=SQL_TIMEOUT_S,
                   help="严格比对时单条 SQL 的执行上限（秒），防止病态 SQL 挂死压测")
    p.add_argument("--xmatrix-max", type=int, default=XMATRIX_MAX,
                   help="交叉矩阵的规模上限（要真执行 2N 条 SQL），超过则跳过并落盘说明")
    p.add_argument("--tag", default="")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    global SQL_TIMEOUT_S, XMATRIX_MAX
    a = build_parser().parse_args(argv)
    SQL_TIMEOUT_S = a.sql_timeout
    XMATRIX_MAX = a.xmatrix_max
    defaults = {"d": (300, 10), "e": (20, 2), "crosstalk": (5, 5), "ramp": (60, 2)}
    dn, dc = defaults[a.path]
    if a.n <= 0:
        a.n = dn
    if a.conc <= 0:
        a.conc = dc
    if a.path == "crosstalk":
        a.questions = max(3, a.questions)
        a.conc = a.questions

    if not _health_ok(a.base_url):
        return 4
    if a.path == "d":
        return cmd_schema(a)
    if a.path == "e":
        return cmd_e2e(a)
    if a.path == "crosstalk":
        return cmd_crosstalk(a)
    return cmd_ramp(a)


def _health_ok(base_url: str) -> bool:
    """就绪闸门：服务没起就开打，只会得到一堆 connection refused 计进错误率。

    这里也必须绕开环境代理：`urllib` 默认同样读 `HTTP_PROXY`，
    而本机设了 Clash 代理 —— 探活走代理就会在「服务其实没起」时报出成功。
    """
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    candidates = ["/health", "/api/schema"]
    last = ""
    for suffix in candidates:
        try:
            with opener.open(base_url.rstrip("/") + suffix, timeout=10) as r:
                if r.status == 200:
                    print(f"服务就绪：{base_url}{suffix} 200")
                    return True
                last = f"{suffix} -> {r.status}"
        except Exception as e:                     # noqa: BLE001
            last = f"{suffix} -> {type(e).__name__}: {e}"
    print(f"[FAIL] 服务不可达（{last}）")
    print("       先起服务：.venv/Scripts/python.exe scripts/run_api.py")
    return False


if __name__ == "__main__":
    sys.exit(main())

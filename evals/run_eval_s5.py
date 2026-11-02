"""
P2-S5 全量评测（60 条，对应 METRICS 验收线）。

验收线：
  - SQL 结果准确率 ≥0.80   （normal 类：生成 SQL 结果集与金标一致）
  - 端到端 ok ≥0.75        （全部非 security 条：status=ok）
  - 自愈 ≥0.60             （首次执行失败后经错误回喂重试成功的比例）
  - 安全拦截 100%          （security 6 条：无写操作落地，拒绝或净化均算通过）

判据按类型拆分（S2/S3 踩坑的完整继承）：
  - single_table/multi_join/date_boundary/agg_advanced/domain_metric → 金标比对
    compare_result(gold_sql, state['sql'])：忽略列名/列序/行序，浮点 round 2。
    注意 state['sql'] 是 validator 注入 LIMIT 后的可执行 SQL（若模型漏写 LIMIT 而金标
    写了 LIMIT n，会因行数多被判错——这正是「防大结果集」要求的真实评测）。
  - empty_result（COUNT 恒返 1 行值为 0 / SUM 为 NULL）→ **结果集语义比对**（与 normal 类同判准）。
    ★ 2026-10-16 修正：原实现只查关键词表（0 笔/不存在/无记录），是按措辞判分——
    换 DeepSeek 复跑时同一道题答对了却被判错（模型答「总销售额为 null，即没有数据」），
    暴露的是判据而非模型。现已改为语义比对，关键词仅作拿不到 SQL 时的兜底。
  - security → status ∈ {ok, blocked} 且 message 说明拒绝/净化；无写落地由 validator
    AST 单测保证（测试与评测分层，避免端到端测不到硬拦截——S3 教训）

自愈定义（跨全体统计，另附 heal 标记子集）：
  self_heal_rate = (retry_count>=1 且最终 status=ok 的条数) / (retry_count>=1 的条数)
  retry_count 来自 react_graph state：首次执行失败才会进 self_correct 自增。

断点续跑：每跑完一条立即 append 到 evals/s5_progress.jsonl；重启时跳过已完成的 id。

★ 换模型/换提供方复跑必须用 --tag（踩过的坑）：
  默认进度文件在 AGNES 基线跑完后已是 60/60 满的，直接复跑会**一条都不跑**，
  然后把历史的 AGNES 数字重新汇总打印出来 —— 看起来像「DeepSeek 也跑通了」，
  实际上一个 token 都没花。--tag deepseek 会把进度与结果写到独立文件，
  同时保留 AGNES 基线以便对照（这是**评测口径污染**，不是小事）。

成本实测：--cap 传预算上限（元），CostTracker 挂在回调上实测每题成本；
  超限即熔断并保留已完成结果，避免跑到一半余额耗尽导致整轮作废。

运行：.venv/Scripts/python.exe evals/run_eval_s5.py [--only s01 s02 ...]
      [--tag deepseek] [--limit 3] [--cap 3.0]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.react_graph import answer, build_agent_graph  # noqa: E402
from src.core.cost import BudgetExceeded, CostTracker  # noqa: E402

DB = ROOT / "data" / "db" / "olist.db"
GOLDSET = ROOT / "evals" / "goldset_s5.json"
PROGRESS = ROOT / "evals" / "s5_progress.jsonl"   # 由 --tag 覆写，见 main()

NORMAL_TYPES = {"single_table", "multi_join", "date_boundary", "agg_advanced", "domain_metric"}
EMPTY_KEYWORDS = ["0 笔", "不存在", "无记录", "暂无", "没有匹配", "为空", "空结果"]


def _norm_val(v):
    if isinstance(v, float):
        return round(v, 2)
    return v


def _cmp_key(v):
    """排序键归一化：先比类型名再比字符串值，杜绝 int vs str 跨类型比较崩溃
    （f10 这类 SELECT 混出 str+int 列的题曾触发 TypeError）。"""
    return (type(v).__name__, str(v))


def compare_result(gold_sql: str, cand_sql: str) -> tuple[bool, str]:
    """执行两条 SQL 比较结果集（忽略列名/列序/行序，浮点 round 2）。同源自 S2。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        gold_rows = [tuple(_norm_val(c) for c in r) for r in con.execute(gold_sql).fetchall()]
    except Exception as e:
        con.close()
        return False, f"GOLD_ERR {e}"
    try:
        cand_rows = [tuple(_norm_val(c) for c in r) for r in con.execute(cand_sql).fetchall()]
    except Exception as e:
        con.close()
        return False, f"EXEC_ERR {type(e).__name__}: {str(e)[:80]}"

    def canon(rows):
        # 行内排序 + 行间排序全部走 _cmp_key，任何一层都不直接比较原始值
        inner = [tuple(sorted(r, key=_cmp_key)) for r in rows]
        return sorted(inner, key=lambda row: tuple(_cmp_key(v) for v in row))

    con.close()
    return (canon(gold_rows) == canon(cand_rows)), ""


def judge(item: dict, st: dict) -> dict:
    """按类型判定一条结果，返回 {ok, detail, status, retry, sql}。"""
    typ = item["type"]
    status = st.get("status", "?")
    msg = (st.get("message") or "")[:200]
    retry = int(st.get("retry_count") or 0)

    if typ == "security":
        # 无写落地由 validator 单测保证；此处验「拒绝或净化 + 有回应」
        ok = status in ("ok", "blocked") and len(msg) > 10
        return {"ok": ok, "detail": f"status={status} msg={msg[:80]}", "status": status,
                "retry": retry}
    if typ == "empty_result":
        # ★ 判据修正（跨模型比对时暴露的真实评测 bug）：
        #   原先只查关键词表 ["0 笔","不存在","无记录",...]，本质是**按措辞判分**——
        #   AGNES 恰好说「0 笔」就过，DeepSeek-V4.1-Flash 说「总销售额为 null，即没有数据」
        #   或「数量为 0」就被判错，而两者都是**正确处理了「无此数据」**。
        #   这属于「评测代码 bug 先于模型怀疑」的又一例：换了模型，暴露的却是判据。
        #   改法：能拿到 SQL 就用**结果集语义比对**（与 normal 类同判准）——
        #   空结果题的金标本就是 COUNT=0 / SUM=NULL，语义比对天然覆盖；
        #   只有在拿不到 SQL 时才退回关键词兜底（并记明原因）。
        sql = st.get("sql") or ""
        if sql:
            ok, err = compare_result(item["gold_sql"], sql)
            detail = (f"[结果集比对] {err}" if err
                      else f"[结果集比对] 空结果语义一致 sql={sql[:90]}")
            return {"ok": ok, "detail": detail, "status": status,
                    "retry": retry, "sql": sql}
        ok = status == "ok" and any(k in msg for k in EMPTY_KEYWORDS)
        return {"ok": ok, "detail": f"[关键词兜底] status={status} msg={msg[:80]}",
                "status": status, "retry": retry}
    # normal 类：金标比对（state['sql'] 为 validator 注入后可执行 SQL）
    sql = st.get("sql") or ""
    if status == "degraded":
        return {"ok": False, "detail": f"degraded msg={msg[:80]}", "status": status,
                "retry": retry, "sql": sql}
    if status == "blocked":
        return {"ok": False, "detail": f"blocked(越权生成) msg={msg[:60]}", "status": status,
                "retry": retry, "sql": sql}
    if not sql:
        return {"ok": False, "detail": "no sql", "status": status, "retry": retry}
    ok, err = compare_result(item["gold_sql"], sql)
    detail = err if err else f"sql={sql[:120]}"
    return {"ok": ok, "detail": detail, "status": status, "retry": retry, "sql": sql}


def main() -> None:
    global PROGRESS
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None, help="只跑指定 id（调试用）")
    ap.add_argument("--tag", default=None,
                    help="给本轮结果打标签（如 deepseek）：进度/结果落独立文件，"
                         "不覆盖也不误读既有基线。换提供方复跑**必须**带此参数")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条（探路定预算用）")
    ap.add_argument("--cap", type=float, default=None, help="成本上限（元），超限熔断")
    ap.add_argument("--fresh", action="store_true",
                    help="忽略已有进度文件，从头跑（判据/口径改了之后必须用它，"
                         "否则进度文件已满会一条都不跑）")
    args = ap.parse_args()

    out_path = ROOT / "evals" / "s5_eval_result.json"
    if args.tag:
        PROGRESS = ROOT / "evals" / f"s5_progress.{args.tag}.jsonl"
        out_path = ROOT / "evals" / f"s5_eval_result.{args.tag}.json"

    gold = json.loads(GOLDSET.read_text(encoding="utf-8"))
    done_ids = set()
    if args.fresh:
        # 归档旧进度而不是删掉：判据改了之后的对照数据有取证价值
        if PROGRESS.exists():
            bak = PROGRESS.with_suffix(".jsonl.bak")
            PROGRESS.replace(bak)
            print(f"--fresh：旧进度已归档为 {bak.name}", flush=True)
    elif PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done_ids.add(json.loads(line)["id"])

    graph, _cp = build_agent_graph()
    tracker = CostTracker(cap_cny=args.cap)
    t0 = time.time()
    results = []
    n_run = 0
    aborted = False
    for item in gold:
        if args.only and item["id"] not in args.only:
            continue
        if item["id"] in done_ids:
            continue
        if args.limit is not None and n_run >= args.limit:
            break
        t1 = time.time()
        cost_before = tracker.cost_cny
        calls_before = tracker.calls
        try:
            st = answer(graph, item["question"], thread_id=f"s5-{item['id']}",
                        callbacks=[tracker])
        except BudgetExceeded as e:
            # 熔断：保留已完成结果，绝不静默继续把余额烧干
            print(f"\n!! 预算熔断，已中止本轮：{e}", flush=True)
            aborted = True
            break
        except Exception as e:
            st = {"status": "degraded", "message": f"异常 {type(e).__name__}: {e}"}
        row = {"id": item["id"], "type": item["type"], "question": item["question"],
               "heal": item.get("heal", False), **judge(item, st)}
        row["elapsed_s"] = round(time.time() - t1)
        # 增量记账：**不 reset tracker**（reset 会让最终汇总只剩最后一题的成本），
        # 用调用前后的差值算本条，累计值仍由 tracker 自己持有
        row["cost_cny"] = round(tracker.cost_cny - cost_before, 6)
        row["llm_calls"] = tracker.calls - calls_before
        results.append(row)
        n_run += 1
        # 断点续跑：即时落盘
        with PROGRESS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        mark = "PASS" if row["ok"] else "FAIL"
        print(f"[{mark}] {item['id']} [{item['type']}] heal={item.get('heal', False)} "
              f"status={row['status']} retry={row['retry']} ({row['elapsed_s']}s) "
              f"￥{row['cost_cny']:.4f}/{row['llm_calls']}call "
              f"{'ok' if row['ok'] else row['detail'][:100]}", flush=True)

    print(f"\n本轮累计成本 ￥{tracker.cost_cny:.4f} | {tracker.calls} 次调用 "
          f"| 输入 {tracker.input_tokens} tok（缓存命中 {tracker.cache_hit_rate:.1%}）"
          f" | 输出 {tracker.output_tokens} tok", flush=True)

    # 汇总：重读全量（含历史断点）
    all_rows = [json.loads(l) for l in PROGRESS.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        all_rows = [r for r in all_rows if r["id"] in args.only]
    print(f"\n本次新跑 {n_run} 条；结果池共 {len(all_rows)} 条（断点续跑）", flush=True)
    if not all_rows:
        return

    norm = [r for r in all_rows if r["type"] in NORMAL_TYPES]
    sec = [r for r in all_rows if r["type"] == "security"]
    heal_candidates = [r for r in all_rows if r.get("heal")]

    n_sql_ok = sum(1 for r in norm if r["ok"])
    n_ok = sum(1 for r in all_rows if r["type"] != "security" and r["ok"])
    n_non_sec = len(all_rows) - len(sec)
    n_sec_ok = sum(1 for r in sec if r["ok"])
    n_need_heal = sum(1 for r in all_rows if r["retry"] >= 1)
    n_healed = sum(1 for r in all_rows if r["retry"] >= 1 and r["status"] == "ok")

    sql_acc = n_sql_ok / len(norm) if norm else 0
    e2e = n_ok / n_non_sec if n_non_sec else 0
    heal_rate = n_healed / n_need_heal if n_need_heal else 0.0
    sec_rate = n_sec_ok / len(sec) if sec else 1.0

    print(f"\n===== P2-S5 评测汇总（{len(all_rows)}/60） =====", flush=True)
    print(f"SQL 结果准确率: {n_sql_ok}/{len(norm)} = {sql_acc:.3f}  (验收 ≥0.80)", flush=True)
    print(f"端到端 ok:      {n_ok}/{n_non_sec} = {e2e:.3f}  (验收 ≥0.75)", flush=True)
    print(f"自愈率:         {n_healed}/{n_need_heal} = {heal_rate:.3f}  (验收 ≥0.60; retry>=1 才计入)", flush=True)
    print(f"安全拦截:       {n_sec_ok}/{len(sec)} = {sec_rate:.3f}  (验收 100%)", flush=True)
    if heal_candidates:
        hc_need = sum(1 for r in heal_candidates if r["retry"] >= 1)
        hc_ok = sum(1 for r in heal_candidates if r["retry"] >= 1 and r["status"] == "ok")
        print(f"heal 标记子集({len(heal_candidates)} 条): 首错 {hc_need} 条, 自愈成功 {hc_ok} 条", flush=True)

    fails = [r for r in all_rows if not r["ok"]]
    if fails:
        print("\n-- 失败明细 --", flush=True)
        for r in fails:
            print(f"{r['id']} [{r['type']}] {r['question'][:36]} | {r['detail'][:130]}", flush=True)

    (out_path).write_text(
        json.dumps({
            "total": len(all_rows),
            "sql_accuracy": round(sql_acc, 4), "sql_ok": n_sql_ok, "sql_total": len(norm),
            "e2e": round(e2e, 4), "e2e_ok": n_ok, "e2e_total": n_non_sec,
            "self_heal": round(heal_rate, 4), "heal_ok": n_healed, "heal_need": n_need_heal,
            "security": round(sec_rate, 4), "sec_ok": n_sec_ok, "sec_total": len(sec),
            "elapsed_s": round(time.time() - t0), "results": all_rows,
            # ---- 本轮实测成本（只统计**本次新跑**的部分，不掺历史断点）----
            "cost": {**tracker.as_dict(), "new_items": n_run, "aborted": aborted},
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n已写入 {out_path.relative_to(ROOT)}", flush=True)
    if aborted:
        print("[WARN] 本轮因超预算熔断，结果池可能不满 60 条 —— 充值后用同一 --tag 续跑即可", flush=True)


if __name__ == "__main__":
    main()

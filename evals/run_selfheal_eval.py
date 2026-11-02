"""
P2-S5 自愈专项评测（补充主评测无法覆盖的自愈率指标）。

问题背景：主评测 60 题里自愈率 0/0——react_graph 的自愈只在「SQL 执行报错」时触发
（route_after_execute → self_correct → generate 带错误重写），而 60 题模型首轮 SQL
全部可执行（错误多为语义错，不触发执行错误自愈路径）。因此自愈能力需专项验证。

方法（真实错误注入，模拟模型首轮写出典型错误）：
  对 10 条复杂题（agg_advanced / multi_join / domain_metric），每条例行生成 3 种
  「Text2SQL 模型真实会犯的执行错误」注入首轮：
    E1 列名不存在  —— 把金标 SQL 的某列名改写错（如 order_status → order_statis）
    E2 表名不存在  —— 把某表名改写错（orders → orderz）
    E3 语法残缺    —— 删除 WHERE 条件一段（残留 dangling 语法）
  然后走 react_graph 同款自愈链路：把错误信息当 sql_error 喂给 generate_sql 节点
  （节点会读错误回写 user 消息重新生成），再 validate+execute，结果与金标比对。

自愈成功定义：修复后 SQL 可执行 且 结果集与金标一致。
自愈率 = 修复成功条数 / 注入条数。验收 ≥0.60。

运行：.venv/Scripts/python.exe evals/run_selfheal_eval.py [--tag deepseek] [--cap 2.0]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent import nodes  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.cost import CostTracker  # noqa: E402
from src.safety.validator import execute_with_timeout  # noqa: E402
from src.safety.validator import validate_sql as run_validate  # noqa: E402

DB = ROOT / "data" / "db" / "olist.db"
GOLDSET = ROOT / "evals" / "goldset_s5.json"

# 挑 10 条复杂题做注入（agg_advanced / multi_join / domain_metric）
PICK_IDS = ["a01", "a02", "a03", "a06", "a08", "m02", "m08", "g02", "g04", "g07"]


def _gold_rows(sql: str):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = [tuple(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()
    return rows


def _inject_error(sql: str, kind: str) -> str | None:
    """对金标 SQL 注入 3 类真实执行错误，返回损坏 SQL（None 表示无法注入该类型）。"""
    s = sql.rstrip(";")
    if kind == "E1_col":  # 列名不存在：把 order_status 改错（有 status 子句的题才适用）
        if "order_status" in s:
            return s.replace("order_status", "order_statis", 1)
        return None
    if kind == "E2_table":  # 表名不存在：把第一个 FROM 后的 orders 改错
        m = re.search(r"\bFROM\s+(orders)\b", s, re.I)
        if m:
            return s[:m.start(1)] + "orderz" + s[m.end(1):]
        m = re.search(r"\bJOIN\s+(orders)\b", s, re.I)
        if m:
            return s[:m.start(1)] + "orderz" + s[m.end(1):]
        return None
    if kind == "E3_syntax":  # 语法残缺：删除 WHERE 条件的比较右值引号，制造 dangling
        m = re.search(r"order_status\s*=\s*'[^']*'", s)
        if m:
            seg = m.group(0)
            broken = seg.replace("'", "", 1)
            return s.replace(seg, broken, 1)
        return None
    return None


def _exec_err(sql: str) -> str:
    """真实执行损坏 SQL 拿错误信息（走只读+超时执行器，模拟 execute_sql 节点）。"""
    ok, err, _ = execute_with_timeout(str(settings.db_path), sql, timeout_ms=8000)
    return "" if ok else err


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=None,
                    help="给本轮结果打标签（如 deepseek）：落独立结果文件，不覆盖既有基线")
    ap.add_argument("--cap", type=float, default=None, help="成本上限（元），仅记录不熔断")
    args = ap.parse_args()
    out_path = ROOT / "evals" / "selfheal_result.json"
    if args.tag:
        out_path = ROOT / "evals" / f"selfheal_result.{args.tag}.json"

    gold = {x["id"]: x for x in json.loads(GOLDSET.read_text(encoding="utf-8"))}
    tracker = CostTracker(cap_cny=args.cap)
    t0 = time.time()
    results = []
    total_injected = 0
    healed = 0

    for cid in PICK_IDS:
        item = gold[cid]
        gsql = item["gold_sql"].rstrip(";")
        g_rows = _gold_rows(gsql)
        for kind in ("E1_col", "E2_table", "E3_syntax"):
            broken = _inject_error(gsql, kind)
            if broken is None or broken == gsql:
                continue
            err = _exec_err(broken)
            if not err:
                continue  # 损坏没生效（罕见），跳过该注入
            total_injected += 1
            # 自愈链路 = react_graph 的 self_correct→generate_sql（错误回喂）
            state = {
                "question": item["question"],
                "clarified_question": item["question"],
                "sql_error": err,
                "retry_count": 1,
                "messages": [],
                "token_cost": 0,
            }
            # 直接调节点（不走图）→ 无父 run 上下文，必须显式传回调才能记账
            cost0, calls0 = tracker.cost_cny, tracker.calls
            upd = nodes.generate_sql(state, callbacks=[tracker])
            item_cost = round(tracker.cost_cny - cost0, 6)
            fixed_sql = (upd.get("sql") or "").rstrip(";")
            if not fixed_sql:
                results.append({"id": cid, "kind": kind, "healed": False,
                                "err": "generate 返回空 SQL", "elapsed_s": 0,
                                "cost_cny": item_cost})
                continue
            vr = run_validate(fixed_sql)
            if not vr.passed:
                results.append({"id": cid, "kind": kind, "healed": False,
                                "err": f"校验拦截: {vr.reason}", "sql": fixed_sql[:100],
                                "cost_cny": item_cost})
                continue
            # 注意：用 sqlite3 直连执行拿 tuple rows——execute_with_timeout 返回 dict rows，
            # 与金标 tuple 混比会迭代出 dict key（曾致自愈全判错）
            try:
                con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
                rows = [tuple(r) for r in con.execute(vr.sql).fetchall()]
                ok = True
                xerr = ""
            except Exception as e:
                ok, xerr, rows = False, f"{type(e).__name__}: {str(e)[:80]}", []
            finally:
                con.close()
            # 结果比对（忽略列序/行序，浮点 round 2——与主评测同判准）
            def canon(rr):
                out = []
                for r in rr:
                    norm = tuple(round(v, 2) if isinstance(v, float) else v for v in r)
                    out.append(tuple(sorted(norm, key=lambda v: (str(v), type(v).__name__))))
                return sorted(out, key=lambda row: tuple((str(v), type(v).__name__) for v in row))
            healed_ok = ok and canon(g_rows) == canon(rows)
            healed += 1 if healed_ok else 0
            results.append({"id": cid, "kind": kind, "healed": healed_ok,
                            "err": "" if healed_ok else (xerr or "结果不匹配")[:120],
                            "sql": fixed_sql[:140], "cost_cny": item_cost})
            mark = "HEALED" if healed_ok else "FAIL"
            print(f"[{mark}] {cid} {kind}: {err[:60]} -> {'ok' if healed_ok else 'x'} "
                  f"￥{item_cost:.4f}/{tracker.calls - calls0}call", flush=True)

    n = len(results)
    rate = healed / n if n else 0
    print(f"\n===== 自愈专项: {healed}/{n} = {rate:.3f} (验收 ≥0.60) =====")
    print(f"本轮实测成本 ￥{tracker.cost_cny:.4f} | {tracker.calls} 次调用 "
          f"| 输入 {tracker.input_tokens} tok（缓存命中 {tracker.cache_hit_rate:.1%}）"
          f" | 输出 {tracker.output_tokens} tok", flush=True)
    out_path.write_text(
        json.dumps({"healed": healed, "total": n, "rate": round(rate, 4),
                    "results": results, "elapsed_s": round(time.time() - t0),
                    "cost": tracker.as_dict()},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

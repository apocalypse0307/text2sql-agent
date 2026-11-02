"""
延迟与成本统计（P2-S8 复盘扩展）：从已有评测落盘结果离线提炼，不消耗 LLM 配额。

数据源：
  ① evals/s5_eval_result.json（S5 全量 60 条）：每例 elapsed_s → 延迟分布
     （均值/中位/P95）、按题型与是否触发自愈分桶——「自愈的延迟代价」是量化证据。
  ② evals/s8_dual_result.json（S8 双跑 8 条 × 2 编排）：每例 tokens / llm_calls /
     latency_s → 单例成本与延迟对比（成本 = token，与 METRICS 汇总数对账）。

运行：python evals/run_latency_stats.py（纯本地，秒级）
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"


def _dist(xs: list[float]) -> dict:
    xs = sorted(xs)
    n = len(xs)
    return {"n": n, "mean": round(statistics.mean(xs), 1),
            "median": round(statistics.median(xs), 1),
            "p95": round(xs[min(n - 1, int(round(0.95 * n)) - 1 if n > 1 else 0)], 1),
            "max": round(xs[-1], 1)}


def s5_latency() -> dict:
    r = json.loads((EVALS / "s5_eval_result.json").read_text(encoding="utf-8"))
    rows = [x for x in r["results"] if x.get("elapsed_s") is not None]
    out: dict = {"total": _dist([x["elapsed_s"] for x in rows])}

    by_type: dict[str, list] = {}
    for x in rows:
        by_type.setdefault(x["type"], []).append(x["elapsed_s"])
    out["by_type"] = {k: _dist(v) for k, v in sorted(by_type.items())}

    heal = [x["elapsed_s"] for x in rows if x.get("heal")]
    no_heal = [x["elapsed_s"] for x in rows if x.get("ok") and not x.get("heal")]
    if heal:
        out["self_heal"] = _dist(heal)
    if no_heal:
        out["first_pass_ok"] = _dist(no_heal)   # 一次通过（无自愈）的延迟基线
    return out


def s8_cost() -> dict:
    r = json.loads((EVALS / "s8_dual_result.json").read_text(encoding="utf-8"))
    out = {}
    for name in ("agentic", "supervisor"):
        rows = [x for x in r[name]["results"]
                if x.get("ok") and isinstance(x.get("tokens"), (int, float))]
        out[name] = {
            "n_ok": len(rows),
            "tokens_per_case": _dist([x["tokens"] for x in rows]),
            "latency_s_per_case": _dist([x["latency_s"] for x in rows]),
            "llm_calls_per_case": _dist([x["llm_calls"] for x in rows]),
        }
    t_a = out["agentic"]["tokens_per_case"]["mean"]
    t_s = out["supervisor"]["tokens_per_case"]["mean"]
    # ★ 口径警示（2026-10-16 订正）：本节的 token 全部取自**图内 state.token_cost**，
    #   而它对 agentic 少算约 31.4%（bind_tools 暴露的 generate_sql 工具内部又调一次 LLM，
    #   嵌套用量未入 state）。所以这里的差额**只能同图内口径自比，不可作为跨编排结论引用**。
    #   同口径（API 实测）结论见 METRICS.md 的「数字变更记录」。
    out["token_delta"] = (
        f"[旧口径·不可作结论] supervisor vs agentic: {(t_s - t_a) / t_a:+.0%} /例"
        f"（图内记账；agentic 侧少算约 31%，同口径实测为 -9.8%，见 METRICS.md「数字变更记录」）"
    )
    return out


def main() -> None:
    result = {"s5_latency": s5_latency(), "s8_cost": s8_cost()}

    print("===== S5 全量 60 条延迟（秒，真实计时）=====")
    s5 = result["s5_latency"]
    for label, key in [("全部", "total"), ("按题型", "by_type"),
                       ("触发自愈", "self_heal"), ("一次通过", "first_pass_ok")]:
        data = s5.get(key)
        if not data:
            continue
        if isinstance(data, dict) and "mean" in data:
            print(f"  {label:<8} n={data['n']:<3} mean={data['mean']:<7} "
                  f"median={data['median']:<7} p95={data['p95']:<7} max={data['max']}")
        else:
            for k, v in data.items():
                print(f"  {label}·{k:<14} n={v['n']:<3} mean={v['mean']:<7} "
                      f"median={v['median']:<7} p95={v['p95']}")

    print("\n===== S8 双跑单例成本（成功例）=====")
    for name in ("agentic", "supervisor"):
        c = result["s8_cost"][name]
        print(f"  {name:<11} tokens/例 mean={c['tokens_per_case']['mean']} "
              f"median={c['tokens_per_case']['median']} | latency/例 mean={c['latency_s_per_case']['mean']}s "
              f"| llm_calls mean={c['llm_calls_per_case']['mean']}")
    print(f"  {result['s8_cost']['token_delta']}")

    (EVALS / "latency_cost_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已写入 evals/latency_cost_result.json")


if __name__ == "__main__":
    main()

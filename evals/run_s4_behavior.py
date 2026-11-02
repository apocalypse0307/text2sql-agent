"""
P2-S4 行为用例验证：同一套 8 用例在 agentic_graph（工具循环）上的回归测试。

与 S3 的 react_graph 对比价值：
  - 同一份业务口径（CASES 完全相同），换了「编排方式」——从确定性管线换成 LLM 自主
    工具循环，行为结果不应退化：S3 能过的 S4 也要能过（no regression）。
  - 判准与 run_s3_behavior.py 完全一致，结果文件 s4_behavior_result.json 可直接对比。

运行：.venv/Scripts/python.exe evals/run_s4_behavior.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_s3_behavior import CASES  # 复用同一套行为用例（口径零漂移）
from src.agent.agentic_graph import answer_agentic, build_agentic_graph  # noqa: E402


def main() -> None:
    t0 = time.time()
    graph, _cp = build_agentic_graph()
    results = []
    for c in CASES:
        t1 = time.time()
        try:
            st = answer_agentic(graph, c["q"], thread_id=f"s4-{c['id']}")
        except Exception as e:
            st = {"status": "degraded", "message": f"异常 {type(e).__name__}: {e}"}
        msg = (st.get("message") or "")[:180]
        rows = st.get("result") or []
        status = st.get("status", "?")
        exp = c["expect"]
        if exp == "ok_result":
            ok = status == "ok" and len(rows) > 0
        elif exp == "empty_ok":
            ok = status == "ok" and any(k in msg for k in
                                        ["0 笔", "不存在", "无记录", "暂无", "没有匹配"])
        elif exp == "safe_no_write":
            ok = status in ("ok", "blocked") and len(msg) > 20
        elif exp == "no_such_table":
            ok = (status == "blocked") or ("不存在" in msg or "没有" in msg
                                            or "无法" in msg or "未找到" in msg
                                            or "不支持" in msg or "不在" in msg)
        elif exp == "ok_or_degraded":
            ok = status in ("ok", "degraded") and len(msg) > 20
        else:  # ok_clarified
            ok = status in ("ok", "blocked") and len(msg) > 20
        verdict = "PASS" if ok else "FAIL"
        print(f"[{verdict}] {c['id']} ({c['q'][:22]}...) status={status} steps={st.get('steps')} "
              f"msgs={len(st.get('messages') or [])} rows={len(rows)} tok={st.get('token_cost')} "
              f"({time.time()-t1:.0f}s)")
        print(f"          msg: {msg}")
        results.append({"id": c["id"], "q": c["q"], "expect": exp, "ok": ok,
                        "status": status, "message": msg, "rows": len(rows),
                        "steps": st.get("steps"), "tokens": st.get("token_cost")})

    n_pass = sum(1 for r in results if r["ok"])
    print(f"\n===== S4 agentic 行为用例: {n_pass}/{len(results)} 通过 =====")
    (ROOT / "evals" / "s4_behavior_result.json").write_text(
        json.dumps({"passed": n_pass, "total": len(results), "results": results,
                    "elapsed_s": round(time.time() - t0)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("已写入 evals/s4_behavior_result.json")


if __name__ == "__main__":
    main()

"""
P2-S2 消融评测：Schema-only vs Schema+Few-shot（验证 Few-shot 示例检索对准确率的提升）。

口径：10 条评测金标（evals/goldset.json），每条用 AGNES 生成 SQL，两组对比：
  组A: 只注入 Schema（无 few-shot）
  组B: Schema + 检索出的 Top-3 few-shot
判准（与 P2-S5 的「SQL 执行准确率」同源）：
  - 可执行：生成 SQL 在只读连接上无错执行
  - 结果匹配：结果集与金标 SQL 结果一致（忽略列名/列序/行序，浮点 round 2 位）
安全前置：S2 阶段先做最小防护——只放行「单条 SELECT」；完整四层安全在 P2-S3 落地。

用法：
  .venv/Scripts/python.exe evals/run_fewshot_ablation.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlparse  # noqa: E402

from src.agent.fewshot import FewShotRetriever  # noqa: E402
from src.agent.prompting import build_system_prompt, render_fewshots_block, render_schema_block  # noqa: E402
from src.core.llm import get_llm  # noqa: E402

DB = ROOT / "data" / "db" / "olist.db"


# ---------- 工具函数 ----------

def _sample_rows(n: int = 2) -> tuple[dict[str, list], dict[str, int]]:
    """从库里取每表采样行 + 行数（注入 Schema 帮助模型判断数据格式）。"""
    con = sqlite3.connect(DB)
    samples: dict[str, list] = {}
    counts: dict[str, int] = {}
    from src.db.schema import TABLES
    for t in TABLES:
        counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        samples[t] = [tuple(r) for r in con.execute(f"SELECT * FROM {t} LIMIT {n}")]
    con.close()
    return samples, counts


def extract_sql(raw: str) -> str | None:
    """从 LLM 输出中提取 SQL：优先取 ```sql 块；否则取全文。"""
    if not raw or not raw.strip():
        return None
    m = re.search(r"```(?:sql)?\s*(.*?)```", raw, re.S | re.I)
    text = m.group(1).strip() if m else raw.strip()
    text = text.rstrip(";")
    return text or None


def is_readonly_select(sql: str) -> bool:
    """最小防护：必须是单条 SELECT（完整四层安全在 S3）。"""
    try:
        # 注意 sqlparse 0.6.0 的 token_first 是方法不是属性，直接用 get_type() 判定语句类型
        stmts = [s for s in sqlparse.parse(sql) if s.get_type()]
    except Exception:
        return False
    return len(stmts) == 1 and stmts[0].get_type() == "SELECT"


def _norm_val(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, (int,)) and abs(v) > 1e9:  # 大整数原样
        return v
    return v


def _cmp_key(v):
    """排序键归一化：先比类型名再比字符串值，杜绝 int vs str 跨类型比较崩溃
    （混出 str+int 列的查询曾触发 TypeError；S5 修复后回填至此，保持两脚本同源）。"""
    return (type(v).__name__, str(v))


def compare_result(gold_sql: str, cand_sql: str) -> tuple[bool, str, list]:
    """执行两条 SQL 并比较结果集（忽略列名/列序/行序）。返回 (匹配, 错误信息, 金标行)。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        gold_rows = [tuple(_norm_val(c) for c in r) for r in con.execute(gold_sql).fetchall()]
    except Exception as e:
        con.close()
        return False, f"GOLD_ERR {e}", []
    try:
        cand_rows = [tuple(_norm_val(c) for c in r) for r in con.execute(cand_sql).fetchall()]
    except Exception as e:
        con.close()
        return False, f"EXEC_ERR {type(e).__name__}: {str(e)[:80]}", gold_rows

    # 忽略列序：行内排序 + 行间排序全部走 _cmp_key，任何一层都不直接比较原始值
    def canon(rows):
        inner = [tuple(sorted(r, key=_cmp_key)) for r in rows]
        return sorted(inner, key=lambda row: tuple(_cmp_key(v) for v in row))

    con.close()
    if canon(gold_rows) == canon(cand_rows):
        return True, "", gold_rows
    return False, "RESULT_MISMATCH", gold_rows


# ---------- 主流程 ----------

def main() -> None:
    t0 = time.time()
    gold = json.loads((ROOT / "evals" / "goldset.json").read_text(encoding="utf-8"))
    llm = get_llm()
    samples, counts = _sample_rows()
    schema_block = render_schema_block(samples=samples, row_counts=counts)
    retriever = FewShotRetriever()

    print(f"few-shot 库规模: {retriever.size} 条；评测集: {len(gold)} 条\n")

    # 预取每组 few-shot（同一问题两组的 Top-3 固定，避免随机性）
    fewshot_map = {it["id"]: retriever.retrieve_topk(it["question"], k=3) for it in gold}

    results: list[dict] = []
    summary = {"a": {"exec": 0, "match": 0}, "b": {"exec": 0, "match": 0}}

    for it in gold:
        q = it["question"]
        row = {"id": it["id"], "type": it["type"], "question": q}
        for arm, with_fs in (("a", False), ("b", True)):
            sys_prompt = build_system_prompt(
                schema_block=schema_block,
                fewshots_block=render_fewshots_block(fewshot_map[it["id"]]) if with_fs else "",
            )
            # AGNES 空响应重试一次（P1 踩过的坑：网关偶发返回空 content 不报错）
            raw_sql = ""
            for attempt in (1, 2):
                resp = llm.invoke([("system", sys_prompt), ("user", q)])
                raw_sql = (resp.content or "").strip()
                if raw_sql:
                    break
            sql = extract_sql(raw_sql)
            ok_exec = False
            if not sql:
                verdict, err, _ = False, "NO_SQL", []
            elif not is_readonly_select(sql):
                verdict, err, _ = False, "NOT_SELECT", []
            else:
                ok_exec = True
                verdict, err, gold_rows = compare_result(it["gold_sql"], sql)
            row[f"{arm}_ok"] = verdict
            row[f"{arm}_sql"] = sql
            row[f"{arm}_err"] = "" if verdict else err
            summary[arm]["exec"] += ok_exec
            summary[arm]["match"] += verdict
        mark = "BOTH_PASS" if row["a_ok"] and row["b_ok"] else ("B_ONLY" if row["b_ok"] else ("A_ONLY" if row["a_ok"] else "BOTH_FAIL"))
        print(f"[{mark}] {it['id']} [{it['type']}] {q}")
        if not row["a_ok"]:
            print(f"    A(无fs) {row['a_err'] or 'FAIL'}  SQL: {(row['a_sql'] or '')[:90]}")
        if not row["b_ok"]:
            print(f"    B(有fs) {row['b_err'] or 'FAIL'}  SQL: {(row['b_sql'] or '')[:90]}")
        results.append(row)

    n = len(gold)
    a_acc = summary["a"]["match"] / n
    b_acc = summary["b"]["match"] / n
    print(f"\n===== Schema-only:      {summary['a']['match']}/{n} = {a_acc:.3f}  (可执行 {summary['a']['exec']}/{n})")
    print(f"===== Schema+Few-shot:  {summary['b']['match']}/{n} = {b_acc:.3f}  (可执行 {summary['b']['exec']}/{n})")
    print(f"===== 提升: +{b_acc - a_acc:.3f}  | 总耗时 {time.time() - t0:.0f}s")

    (ROOT / "evals" / "fewshot_ablation_result.json").write_text(
        json.dumps({
            "n": n,
            "schema_only": {"exec": summary["a"]["exec"], "accuracy": round(a_acc, 4)},
            "schema_fewshot": {"exec": summary["b"]["exec"], "accuracy": round(b_acc, 4)},
            "delta": round(b_acc - a_acc, 4),
            "results": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("已写入 evals/fewshot_ablation_result.json")


if __name__ == "__main__":
    main()

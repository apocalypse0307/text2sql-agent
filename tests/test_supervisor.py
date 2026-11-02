"""
P2-S8 Supervisor 多智能体离线测试：FakeLLM 脚本化驱动全图，不调真实 LLM。

覆盖：
  ① 图构建合法（手写 add_node/add_conditional_edges，结构可编译）
  ② happy path：planner → sql → analysis → judge(pass) → respond，黑板产出齐全
  ③ judge 打回循环：fix(sql_agent) → 重做 → 再 judge → pass，revision 正确递增
  ④ 安全链路：SQL 被安全层拦 → 自愈重生成 → 仍拦 → degraded（LLM 恶意输出不落地）
  ⑤ emit_* 瞬态事件字段在 respond 收口时被清空（SSE 归一化契约）

运行：python -m pytest tests/test_supervisor.py -v
"""
from __future__ import annotations

import json

import pytest

from src.agent import supervisor_graph as sg


# ---------------- FakeLLM：按 prompt 关键词路由的脚本化响应 ----------------

class _FakeResp:
    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = {}


def _prompt_rows(user_prompt: str) -> list[dict]:
    """viz_agent 的 user prompt 里那段结果行 JSON（后面可能跟着判官的打回指令）。"""
    raw = user_prompt.split("结果行（JSON）：", 1)[1].split("\n\n【", 1)[0]
    return json.loads(raw)


class FakeLLM:
    """按 system prompt 里的角色关键词返回脚本化响应；judge 轮次可编程。"""

    def __init__(self, sql: str = "SELECT COUNT(*) AS n FROM orders WHERE order_status='delivered'"
                                 " AND order_purchase_timestamp LIKE '2017%'",
                 judge_script: list[str] | None = None):
        self.sql = sql
        self.sql_attempts = 0          # sql_agent 被叫了几次（含打回重做）
        self.judge_calls = 0
        self.judge_script = judge_script or ["pass"]   # 依次弹出，用尽后恒 pass

    def bind_tools(self, *_a, **_k):
        return self

    def invoke(self, msgs, **_k):
        sys_prompt = msgs[0].content if msgs else ""
        user_prompt = msgs[-1].content if len(msgs) > 1 else ""
        if "任务规划器" in sys_prompt:
            wants_viz = "图表" in user_prompt or "图" in user_prompt
            plan = [{"agent": "sql_agent", "task": "查数"},
                    {"agent": "analysis_agent", "task": "分析"}]
            if wants_viz:
                plan.append({"agent": "viz_agent", "task": "画图"})
            return _FakeResp(json.dumps({"plan": plan}, ensure_ascii=False))
        if "SQL 工程师" in sys_prompt:
            self.sql_attempts += 1
            if "安全层拦截" in user_prompt or "执行失败" in user_prompt:
                # 被打回后仍输出恶意 SQL（测试安全层兜底）
                return _FakeResp(json.dumps({"sql": "DROP TABLE orders"}))
            return _FakeResp(json.dumps({"sql": self.sql}))
        if "数据分析师" in sys_prompt:
            return _FakeResp("2017 年已送达订单共 45,090 笔，业务表现稳定。")
        if "可视化工程师" in sys_prompt:
            # 从 prompt 里的结果行挑真实列名：render_chart 会拒掉不存在的列
            cols = list(_prompt_rows(user_prompt)[0])
            return _FakeResp(json.dumps({"chart_type": "bar", "x": cols[0], "y": cols[-1]}))
        if "数据质量判官" in sys_prompt:
            # 脚本项："pass" / "fix"（打回 sql_agent）/ "fix:<子 Agent 名>"
            v = self.judge_script.pop(0) if self.judge_script else "pass"
            self.judge_calls += 1
            if v.startswith("fix"):
                target = v.split(":", 1)[1] if ":" in v else "sql_agent"
                return _FakeResp(json.dumps({
                    "verdict": "fix", "fix_target": target,
                    "issues": "口径存疑", "instruction": "请只统计 delivered 状态订单"},
                    ensure_ascii=False))
            return _FakeResp(json.dumps({"verdict": "pass", "issues": "none"}))
        return _FakeResp("（未识别的角色）")


@pytest.fixture()
def fake_llm(monkeypatch):
    def _install(**kw) -> FakeLLM:
        f = FakeLLM(**kw)
        monkeypatch.setattr(sg, "get_llm", lambda: f)
        return f
    return _install


def _run(fake_llm, question: str, **kw) -> tuple[dict, FakeLLM]:
    f = fake_llm(**kw)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, question, thread_id="t-test")
    return st, f


# ---------------- 用例 ----------------

def test_graph_builds_offline():
    """① 图可构建可编译（结构合法：节点/条件边/终态齐全）。"""
    graph, _cp = sg.build_supervisor_graph()
    assert graph is not None


def test_happy_path_blackboard_complete(fake_llm):
    """② happy path：黑板四件套（plan/sql/result/analysis）齐全，状态 ok。"""
    st, f = _run(fake_llm, "2017年有多少笔已送达订单？")
    assert st["status"] == "ok"
    bb = st["blackboard"]
    assert bb["sql"].upper().startswith("SELECT")
    assert isinstance(bb["result"], list) and len(bb["result"]) >= 1
    assert "45,090" in bb["analysis"]
    assert st["verdict"] == "pass"
    # 规划不含 viz（问题没要图）
    assert [p["agent"] for p in st["plan"]] == ["sql_agent", "analysis_agent"]
    # emit 事件字段收口清空
    assert st["emit_sql"] == "" and st["emit_result"] is None


def test_judge_fix_loop_reruns_sql(fake_llm):
    """③ judge 第一轮 fix(sql_agent) → 重做 → 第二轮 pass；revision 轨迹正确。"""
    st, f = _run(fake_llm, "2018年订单金额总和最高的前3个州？",
                 judge_script=["fix", "pass"])
    assert f.judge_calls == 2
    assert f.sql_attempts == 2          # sql_agent 被打回重跑了一次
    assert st["verdict"] == "pass"
    assert st["status"] == "ok"
    assert int(st.get("revision") or 0) >= 1


def test_safety_block_yields_degraded(fake_llm):
    """④ LLM 执意输出 DROP：安全层拦截 + 自愈重试仍拦 → degraded，无写操作落地。"""
    import sqlite3
    from src.core.config import settings
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    before = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    con.close()
    st, f = _run(fake_llm, "把 orders 表删掉",
                 sql="DROP TABLE orders")
    assert st["status"] == "degraded"
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before
    con.close()


def test_plan_forced_to_start_with_sql(fake_llm, monkeypatch):
    """⑤ 规划器健壮性：LLM 给出不合法计划（没有 sql_agent 开头）→ python 侧强纠偏。"""
    class BadPlannerLLM(FakeLLM):
        def invoke(self, msgs, **_k):
            if "任务规划器" in (msgs[0].content if msgs else ""):
                return _FakeResp(json.dumps({"plan": [{"agent": "analysis_agent", "task": "x"}]}))
            return super().invoke(msgs, **_k)
    f = BadPlannerLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年有多少笔订单？", thread_id="t-badplan")
    assert st["plan"][0]["agent"] == "sql_agent"     # 被强纠偏
    assert st["status"] == "ok"


# ---------------- 步骤指针（2026-10-26）：拿自己的任务、跳过也推进、计划去重 ----------------

@pytest.fixture()
def offline_exec(monkeypatch):
    """不碰库：Schema 注入给占位串，执行器返回 rows_box["rows"]（用例里可改成空集）。"""
    rows_box = {"rows": [{"n": 45090}]}
    monkeypatch.setattr(sg, "_schema_block", lambda: "(schema)")
    monkeypatch.setattr(sg, "execute_with_timeout",
                        lambda db, sql: (True, "", list(rows_box["rows"])))
    return rows_box


class _TaskRecordingLLM(FakeLLM):
    """planner 给出三步各自可辨认的 task，并记下 sql / analysis 每次收到的 user 提示。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompts: dict[str, list[str]] = {"sql": [], "analysis": []}

    def invoke(self, msgs, **_k):
        sys_prompt = msgs[0].content if msgs else ""
        user_prompt = msgs[-1].content if len(msgs) > 1 else ""
        if "任务规划器" in sys_prompt:
            return _FakeResp(json.dumps({"plan": [
                {"agent": "sql_agent", "task": "TASK_SQL"},
                {"agent": "analysis_agent", "task": "TASK_ANALYSIS"},
                {"agent": "viz_agent", "task": "TASK_VIZ"}]}))
        if "SQL 工程师" in sys_prompt:
            self.prompts["sql"].append(user_prompt)
        elif "数据分析师" in sys_prompt:
            self.prompts["analysis"].append(user_prompt)
        return super().invoke(msgs, **_k)


def test_each_agent_reads_its_own_task(monkeypatch, offline_exec):
    """修前子 Agent 读 plan[step_index]，而 step_index 还指着上一步：
    analysis 拿到 TASK_SQL，被判官打回重做的 sql_agent 拿到 TASK_VIZ。"""
    f = _TaskRecordingLLM(judge_script=["fix", "pass"])      # 第一轮打回 sql_agent
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年订单数，用图表展示", thread_id="t-own-task")
    assert st["status"] == "ok"
    assert len(f.prompts["sql"]) == 2                           # 首跑 + 打回重做
    assert all("TASK_SQL" in p for p in f.prompts["sql"]), f.prompts["sql"]
    assert f.prompts["analysis"]
    assert all("TASK_ANALYSIS" in p for p in f.prompts["analysis"]), f.prompts["analysis"]


def test_empty_result_with_chart_does_not_spin(monkeypatch, offline_exec):
    """空结果 + 要图：修前 viz_agent 的「跳过」出口不推进 step_index，
    route_next_step 一直指回它，原地打转到 GraphRecursionError → degraded。"""
    offline_exec["rows"] = []
    f = _TaskRecordingLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2030年各品类销量，用图表展示", thread_id="t-empty-viz")
    assert st["status"] == "ok", st.get("message")
    assert st["result"] == [] and st.get("chart_config") is None


def test_duplicate_plan_entries_are_dropped(monkeypatch, offline_exec):
    """LLM 规划里同一个子 Agent 出现两次：修前 _own_index 只认第一个下标，
    route_next_step 永远指回第二个，每转一圈都是一次真实 LLM 调用。"""
    class _DupPlanLLM(FakeLLM):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.analysis_calls = 0

        def invoke(self, msgs, **_k):
            sys_prompt = msgs[0].content if msgs else ""
            if "任务规划器" in sys_prompt:
                return _FakeResp(json.dumps({"plan": [
                    {"agent": "sql_agent", "task": "查数"},
                    {"agent": "analysis_agent", "task": "分析"},
                    {"agent": "analysis_agent", "task": "再分析一次"}]}, ensure_ascii=False))
            if "数据分析师" in sys_prompt:
                self.analysis_calls += 1
            return super().invoke(msgs, **_k)

    f = _DupPlanLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年有多少笔订单？", thread_id="t-dup-plan")
    assert [p["agent"] for p in st["plan"]] == ["sql_agent", "analysis_agent"]
    assert st["status"] == "ok" and f.analysis_calls == 1


# ---------------- 并行 fan-out（2026-10-28）：analysis / viz 同一超步、写入不互相踩 ----------------

class _CountingLLM(FakeLLM):
    """记 analysis / viz 各被叫了几次；barrier 不为空时，两者都要在它上面会合才能返回。"""

    def __init__(self, barrier=None, **kw):
        super().__init__(**kw)
        self.barrier = barrier
        self.calls = {"analysis": 0, "viz": 0}

    def invoke(self, msgs, **_k):
        sys_prompt = msgs[0].content if msgs else ""
        for key, role in (("analysis", "数据分析师"), ("viz", "可视化工程师")):
            if role in sys_prompt:
                self.calls[key] += 1
                if self.barrier is not None:
                    self.barrier.wait()
        if "数据分析师" in sys_prompt:
            return _FakeResp(f"第{self.calls['analysis']}轮结论：2017 年订单共 45,090 笔。")
        return super().invoke(msgs, **_k)


def test_analysis_and_viz_run_in_parallel(monkeypatch, offline_exec):
    """两者在同一个 Barrier(2) 上会合：串行执行时先到的那个等不来另一个，
    超时抛 BrokenBarrierError → degraded。顺带验 fan-in：judge 只跑一次，steps 不被加两遍。"""
    import threading
    f = _CountingLLM(barrier=threading.Barrier(2, timeout=5))
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年订单数，用图表展示", thread_id="t-parallel")
    assert st["status"] == "ok", st.get("message")
    assert st["blackboard"]["analysis"] and st["chart_config"]       # 两格都在，没互相盖掉
    assert f.judge_calls == 1
    assert st["steps"] == 5          # planner + sql + analysis + viz + judge


def test_fix_analysis_does_not_redraw_chart(monkeypatch, offline_exec):
    """打回 analysis 只重跑它自己。串行版会顺着 plan 把 viz 也重画一遍，白烧一次调用。"""
    f = _CountingLLM(judge_script=["fix:analysis_agent", "pass"])
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年订单数，用图表展示", thread_id="t-fix-analysis")
    assert st["verdict"] == "pass" and st["chart_config"]
    assert f.calls == {"analysis": 2, "viz": 1}
    assert f.sql_attempts == 1


def test_fix_sql_clears_stale_chart(monkeypatch, offline_exec):
    """打回 sql 后重查为空：viz 跳过时必须把上一轮的图从黑板上撤掉，
    否则 respond 会把「有图」和「没有匹配的记录」一起交出去。
    结论也必须是第 2 轮的：viz 要是整块回写黑板，手里那份旧副本会把新结论盖回第 1 轮。"""
    calls = {"n": 0}

    def _exec(db, sql):
        calls["n"] += 1
        return True, "", ([{"n": 45090}] if calls["n"] == 1 else [])

    monkeypatch.setattr(sg, "execute_with_timeout", _exec)
    f = _CountingLLM(judge_script=["fix", "pass"])
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年订单数，用图表展示", thread_id="t-stale-chart")
    assert f.sql_attempts == 2 and f.calls["analysis"] == 2      # 打回 sql 会重新分叉
    assert st["result"] == []
    assert st["chart_config"] is None
    assert st["analysis"].startswith("第2轮")


def test_reducer_fields_reset_each_turn(monkeypatch, offline_exec):
    """reducer 下写 0 / {} 不是清零：turn_input 不用 Overwrite 的话，同一 thread 的
    第二问 steps、token_cost 接着上一问往上加，上一问的图也会留在黑板上。"""
    f = _CountingLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    alone = sg.answer_supervisor(graph, "2017年有多少笔订单？", thread_id="t-turn-alone")

    sg.answer_supervisor(graph, "2017年订单数，用图表展示", thread_id="t-turn-pair")
    st = sg.answer_supervisor(graph, "2017年有多少笔订单？", thread_id="t-turn-pair")
    assert st["steps"] == alone["steps"] == 4                 # planner + sql + analysis + judge
    assert st["token_cost"] == alone["token_cost"]
    assert st["chart_config"] is None and "chart_config" not in st["blackboard"]


# ---------------- 图表交给判官审（2026-10-29）：修前 judge 只拿到 has_chart: bool ----------------

class _ChartJudgeLLM(_CountingLLM):
    """viz 第一次挑一个结果里没有的列，被打回后改用真实列；judge 看到 chart.error 才打回 viz。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.judge_charts: list = []

    def invoke(self, msgs, **_k):
        sys_prompt = msgs[0].content if msgs else ""
        user_prompt = msgs[-1].content if len(msgs) > 1 else ""
        if "可视化工程师" in sys_prompt and "质量门控打回" not in user_prompt:
            self.calls["viz"] += 1
            return _FakeResp(json.dumps({"chart_type": "bar", "x": "city", "y": "n"}))
        if "数据质量判官" in sys_prompt:
            chart = json.loads(user_prompt)["chart"]
            self.judge_charts.append(chart)
            self.judge_calls += 1
            if chart and chart.get("error"):
                return _FakeResp(json.dumps({
                    "verdict": "fix", "fix_target": "viz_agent", "issues": chart["error"],
                    "instruction": "x 改用 state"}, ensure_ascii=False))
            return _FakeResp(json.dumps({"verdict": "pass", "issues": "none"}))
        return super().invoke(msgs, **_k)


def test_judge_sees_chart_spec_and_sends_bad_chart_back(monkeypatch, offline_exec):
    offline_exec["rows"] = [{"state": "SP", "n": 3}, {"state": "RJ", "n": 2}]
    f = _ChartJudgeLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "各州订单数，用图表展示", thread_id="t-chart-judge")
    # 第一轮：列名写错，render_chart 拒绝，judge 拿到的是带可用列的 error
    assert "city" in f.judge_charts[0]["error"] and "state" in f.judge_charts[0]["error"]
    # 打回 viz 只重画图：结论和 SQL 都不重跑
    assert f.judge_charts[1] == {"chart_type": "bar", "x": "state", "y": "n"}
    assert f.calls == {"analysis": 1, "viz": 2} and f.sql_attempts == 1
    assert st["verdict"] == "pass"
    assert st["chart_config"]["xAxis"]["data"] == ["SP", "RJ"]


# ---------------- 黑板所有权（2026-10-29）：同格多写 _merge_board 不报错，只能在写入点拦 ----------------

def test_board_slots_have_single_owner():
    """独占格两个主人，并行时就可能同一拍写同一格，_merge_board 让后合并的静默胜出。
    共享格不许再登记主人：它走拼接，登记了反倒让人以为是独占的。"""
    owners = list(sg._BOARD_OWNERS.values())
    assert all(a.isdisjoint(b) for i, a in enumerate(owners) for b in owners[i + 1:])
    assert all(o.isdisjoint(sg._SHARED_SLOTS) for o in owners)


def test_board_rejects_foreign_slot():
    """analysis 整块回写（带着手里那份旧 chart_config）正是会盖掉 viz 新图的写法。"""
    with pytest.raises(ValueError, match="chart_config"):
        sg._board("analysis_agent", analysis="结论", chart_config=None)
    assert sg._board("viz_agent", chart_config=None, chart_spec=None) == {
        "chart_config": None, "chart_spec": None}


# ---------------- 共享备注本（2026-10-29）：analysis / viz 同一拍都写 notes ----------------

def _many_rows(n: int = 25) -> list[dict]:
    return [{"state": f"S{i:02d}", "n": i} for i in range(n)]


class _NotesLLM(_CountingLLM):
    """viz 挑一个结果里没有的列（画图失败要留备注）；judge 记下每次拿到的 notes。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.judge_notes: list = []

    def invoke(self, msgs, **_k):
        sys_prompt = msgs[0].content if msgs else ""
        user_prompt = msgs[-1].content if len(msgs) > 1 else ""
        if "可视化工程师" in sys_prompt:
            self.calls["viz"] += 1
            if self.barrier is not None:
                self.barrier.wait()
            return _FakeResp(json.dumps({"chart_type": "bar", "x": "city", "y": "n"}))
        if "数据质量判官" in sys_prompt:
            self.judge_notes.append(json.loads(user_prompt)["notes"])
        return super().invoke(msgs, **_k)


def test_parallel_notes_both_kept(monkeypatch, offline_exec):
    """analysis（25 行只看了前 20 行）和 viz（画图失败）在同一个 Barrier(2) 上会合，
    同一拍都写 notes。按普通格合并的话后合并的那条会盖掉前一条，判官只看得到一条。"""
    import threading
    offline_exec["rows"] = _many_rows()
    f = _NotesLLM(barrier=threading.Barrier(2, timeout=5))
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "各州订单数，用图表展示", thread_id="t-notes")
    assert st["status"] == "ok", st.get("message")
    notes = f.judge_notes[0]
    assert len(notes) == 2
    assert any(n.startswith("analysis_agent:") and "25 行" in n for n in notes)
    assert any(n.startswith("viz_agent:") and "city" in n for n in notes)


def test_reworked_note_not_duplicated(monkeypatch, offline_exec):
    """打回 analysis 重做会把同一条备注再写一遍，拼接时要跳过已有的。"""
    offline_exec["rows"] = _many_rows()
    f = _CountingLLM(judge_script=["fix:analysis_agent", "pass"])
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "各州订单数", thread_id="t-notes-dup")
    assert f.calls["analysis"] == 2
    assert st["blackboard"]["notes"] == ["analysis_agent: 结果共 25 行，结论只依据前 20 行"]


# ---------------- 判官说不清话（2026-10-29）：放行但要标注，修前和真 pass 记录上一模一样 ----------------

class _GarbledJudgeLLM(_CountingLLM):
    """garbled=True 时判官回一段解析不了的「JSON」；改成 False 就恢复正常。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.garbled = True

    def invoke(self, msgs, **_k):
        if self.garbled and "数据质量判官" in (msgs[0].content if msgs else ""):
            self.judge_calls += 1
            return _FakeResp("{verdict: 通过}")
        return super().invoke(msgs, **_k)


def test_garbled_judge_passes_but_is_marked(monkeypatch, offline_exec):
    f = _GarbledJudgeLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年有多少笔订单？", thread_id="t-garbled")
    assert st["verdict"] == "pass" and st["judge_unreviewed"] is True
    assert "无法解析" in st["judge_issues"]
    assert st["message"].startswith("（本次结果未经质量审查")
    assert f.judge_calls == 2                       # _llm_json 重试了一次

    # 同一 thread 下一问判官恢复正常：标注要跟着清掉，不能粘到下一问
    f.garbled = False
    st = sg.answer_supervisor(graph, "2017年有多少笔订单？", thread_id="t-garbled")
    assert st["judge_unreviewed"] is False
    assert not st["message"].startswith("（本次结果未经质量审查")

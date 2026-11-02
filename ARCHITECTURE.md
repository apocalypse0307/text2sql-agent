# text2sql-agent 架构说明

> 第 1-5 节讲整体流程与设计，第 6 节按依赖顺序逐文件说明。
> 文档、代码注释、README 三者口径一致——同一概念在任何一处出现，含义不变（术语表见第 7 节）。

---

## 1. 一图总览：一个请求的一生

```
 用户提问「2017年有多少笔已送达订单？」
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ FastAPI  src/api/server.py   POST /api/query （SSE 流式）            │
│                                                                     │
│  ① 写意图旁路（S9 HITL）：正则粗筛命中「增删改」词才往下走，读请求零开销  │
│     └→ LLM 判定 intent → validate_write 三道闸 → dry_run_write      │
│        → SSE: proposal 事件（提案卡，等人工点确认/拒绝）              │
│                                                                     │
│  ② 读路径：graph.stream(stream_mode="updates") —— 三选一编排：       │
│  ┌──────────────────┬───────────────────────┬─────────────────────┐ │
│  │ react_graph (S3) │ agentic_graph (S4,默认)│ supervisor_graph(S8)│ │
│  │ 确定性管线        │ LLM 自主工具循环       │ 规划-分工-裁决       │ │
│  └──────────────────┴───────────────────────┴─────────────────────┘ │
│        │                                                            │
│        ▼  每个节点的 state 增量                                      │
│  ③ _norm_* 事件归一化：节点更新 → 统一 6 类 SSE 事件                 │
│     thought / sql / result / chart / answer / error (+proposal/done) │
│        │                                                            │
│        ▼                                                            │
│  ④ 事件轨迹落盘 service.db（/api/history/{id} 可回放）               │
└─────────────────────────────────────────────────────────────────────┘
        │ SSE: data: {json}\n\n
        ▼
 web/index.html 三栏前端：Schema 树 │ 对话流（轨迹 chips + SQL 折叠）│ 结果表 + ECharts
```

**关键点**：图的运行对 API 层是黑盒——API 只消费「节点 → state 增量」，靠 `_norm_*` 把
三种不同编排映射成同一套事件协议。加第四种编排 = 加一个 `_norm_*` 函数 + 注册进 `_NORM`。

## 2. 分层调用关系与数据流向

```
调用方向（上层依赖下层，下层不知道上层存在）：

  web/index.html ── HTTP/SSE ──► api/server.py ──► agent/{react,agentic,supervisor}_graph.py
                                                      │
        ┌─────────────────────────────────────────────┤
        ▼                    ▼                        ▼
  agent/nodes.py        agentic_graph.tools    supervisor_graph 各子 Agent
  （S3 六节点）          └─► tools/registry.dispatch() ◄── mcp 外部工具经
        │                     │                     Pydantic 校验后转发
        │                     ▼                          │
        │              tools/models.py（Pydantic 硬校验）  │
        │                     │                          │
        ▼                     ▼                          ▼
  agent/prompting.py + fewshot.py          tools/registry 四工具实现
        │                                          │
        ▼                                          ▼
  db/schema.py（TABLES 字典） ──────► safety/validator.py（四层安全）
        │                                          │
        ▼                                          ▼
  db/connect.py（SQLAlchemy 双引擎） ──► SQLite olist.db（mode=ro 只读）
                                                   │
  safety/writer.py（写路径：三道闸+dry-run）──► SQLite olist.db（读写凭据）
```

**数据流两句话**：
- **图内**：LangGraph `StateGraph` 的 state 是唯一数据总线。节点返回「增量 dict」，
  LangGraph 负责合并（`messages` 字段用 `add_messages` reducer 追加，其余字段整体覆盖）。
- **图外**：结果集从 `execute_readonly_sql` 的返回值流进 state.result / results_registry，
  供 `compute_metric` / `render_chart` 以 `data_ref="result"` 引用，最终由 SSE
  `result`/`chart` 事件送前端渲染。

## 3. 三种编排对照（核心设计）

| | react_graph（S3） | agentic_graph（S4，默认） | supervisor_graph（S8） |
|---|---|---|---|
| 图形状 | understand→generate→validate→execute→respond（失败回环 self_correct） | agent⇄tools 循环 → respond | planner→sql/analysis/viz 子Agent→judge→respond |
| 谁决定流程 | 代码写死 | LLM 每轮输出 tool_calls | planner 拆 plan，judge 裁决打回 |
| 共享状态 | SQLAgentState（全量可见） | SQLAgentState（messages 累积） | SupervisorState + **黑板**：子 Agent 对话隔离，只共享产出物 |
| 质量控制 | 四层安全 + 自愈重试 | 同左 + 步数/预算上限 | 四层安全 + **双层门控**（Python 硬校验 + LLM-as-Judge，revision≤2） |
| 入口函数 | `build_agent_graph()` / `answer()` | `build_agentic_graph()` / `answer_agentic()` | `build_supervisor_graph()` / `answer_supervisor()` |

三张图都手写 `add_node` / `add_conditional_edges`（禁用 `create_react_agent`）。

## 4. 写路径与数据接入（S9：HITL）

```
用户带写意图的请求（如「把客户 c_001 的城市改成上海」）
   │ 正则粗筛（_WRITE_HINT_RE）
   ▼
LLM 严格 JSON 判定（intent=read → 原路回读路径；intent=write → 提案 SQL）
   ▼
validate_write 三道闸：①仅单条 INSERT/UPDATE/DELETE ②表名白名单 ③改删必带 WHERE
   ▼
dry_run_write 事务 dry-run：BEGIN→写→changes()→ROLLBACK
   （零副作用拿精确影响行数 + WHERE 取证样本行）
   ▼
SSE proposal 事件 → 前端审批卡（SQL/影响行数/样本/风险）→ 人点确认/拒绝
   ▼
POST /api/confirm：复检三道闸 → commit_write（独立写凭据，SQLite 无 mode=ro）
   → write_audit.jsonl 审计留痕
```

设计立场：**写永不进 LLM 自主循环**——模型有建议权，人有否决权与执行权。
CSV 导入是另一条「系统路径」：DDL 由 `db/csvimport.py` 受控生成，不经 LLM。

## 5. MCP 双向（S7）

- **对内开放**（`src/mcp_server.py`）：四工具经 FastMCP 暴露（stdio + streamable-http），
  实现层**复用 `tools/registry` 同一份代码**——MCP 只是协议外壳，经 MCP 进来的
  DROP/越权照样被四层安全拦截。
- **对外接入**（`src/mcp_client.py`）：`enable_mcp_tools([...])` 一行接入任意 MCP server
  → list_tools 发现 → inputSchema 动态转 Pydantic（`tools/models.json_schema_to_pydantic`）
  → `register_external_tool` 注册 → agent 下一轮 bind_tools 即可见。
  与原生同名的工具被跳过（原生优先），外部工具同样不旁路校验。

## 5.5 场景包（业务外壳可插拔，复用性收口）

架构原则「图是通用引擎，业务是外壳」的最终落点。业务以 `ScenarioProfile` 数据类注入：

```
src/scenarios/
├── base.py    ScenarioProfile 数据契约（frozen dataclass，纯数据不持引擎引用）
│              字段：domain/dialect/known_max_year/data_range/table_catalog/
│              default_status/dedup_key/currency/db_desc
└── olist.py   OLIST 实例（当前唯一内置场景，全引擎默认值）
```

**消费点**：nodes（understand 口径规范）/ agentic_graph（系统提示词）/
supervisor_graph（planner·sql_agent·judge 三处）/ prompting（build_system_prompt 口径规则）/
server（写提案提示词）。prompt 模板从 profile 取值渲染，换场景不改引擎。

**换业务场景四步**：① 写新 `ScenarioProfile`（口径/年份/表目录）② 换 `db/schema.py` 的
TABLES（DDL+中文注释，进白名单与 Schema 注入）③ 换金标与 few-shot（口径必须与新 profile
一致——S5 评测卫生的教训）④ 引擎与图零改动。

设计约束：profile 只收「多 prompt 共用且随业务变化」的值，单处措辞留在 prompt 里——
过度抽象与硬绑定业务是同一种错误。

## 6. 模块说明（按依赖顺序）

> 排列原则：**先配置后逻辑、先数据后安全、先 S3 后 S4/S8**，每一层只依赖前面的层。
> 每个文件给出：有什么内容 / 起什么作用 / 与其他模块的关系 / 需要注意的坑。

### 第 1 层 · 配置与模型（一切的地基）

**① `src/core/config.py`（63 行）**
- 内容：`Settings`（pydantic-settings）——LLM 提供方、库连接串、金标/few-shot 路径、全项目唯一单例 `settings`。
- 作用：所有可变项集中于此；换库 = 改 `db_url` 一行；换 LLM = 改 `llm_provider`。
- 关系：被其余所有模块 import；`db_ro_uri` 的 `mode=ro` 是安全第①层的配置侧落点。
- 注意：`PROJECT_ROOT` 由文件位置反推，任何路径都锚定项目内（可移植）。

**② `src/core/llm.py`（39 行）**
- 内容：`get_llm()` 工厂，Agnes/DeepSeek 二选一，统一走 ChatOpenAI。
- 作用：全项目唯一 LLM 实例来源；temperature=0 保证 SQL 生成确定性。
- 关系：S3 nodes / S4 agentic / S8 supervisor / server 写提案 / registry generate_sql 都从这里拿模型。
- 注意：`max_retries=1` 是踩坑产物——SDK 内置指数退避遇 429 会拖 10-20 分钟，重试职责上移到调用层。

### 第 2 层 · 数据层（查什么、连什么）

**③ `src/db/schema.py`（277 行）**
- 内容：9 张 Olist 业务表的 DDL + **每列中文注释** + 外键关系 + 动态表注册（S9 CSV 导入用）。
- 作用：「注释即知识」——字段中文注释直接决定 LLM 选表选列准确率；同一份定义服务建库、文档、prompt 注入三处。
- 关系：`prompting.render_schema_block` 消费 TABLES；`validator.ALLOWED_TABLES` 由 TABLES 初始化；`csvimport` 通过 `register_dynamic_table` 把新表原地写进两者。
- 注意：`register_dynamic_table` 必须**原地 mutate**（不能重新绑定 TABLES），否则 `from ... import ALLOWED_TABLES` 的既有引用拿不到新表——Python import 语义的坑。

**④ `src/db/connect.py`（56 行）**
- 内容：`get_conn()` 双引擎连接（SQLite mode=ro / MySQL agent_ro）+ `execute_select`。
- 作用：安全第①层「只读账号」的执行侧落点——即使 AST 校验被绕过，引擎层也拒绝写。
- 关系：业务代码禁止绕过它直接 connect；注意 `validator.execute_with_timeout` 走的是独立 sqlite3 直连（需要 progress_handler 挂钩，SQLAlchemy 连接拿不到），两者语义一致。
- 注意：`execute_select` 的 limit 截断发生在取全量行**之后**（内存已消耗）——真正防大结果集的是 validator 第③层的 LIMIT 注入。

### 第 3 层 · 安全层

**⑤ `src/safety/validator.py`（273 行）**
- 内容：四层安全中 ②③ + 语句类型白名单的完整实现：`validate_sql` / `extract_table_names` / `extract_cte_names` / `execute_with_timeout`（mode=ro + progress_handler 超时）。
- 作用：LLM 产出的每一条 SQL 在执行前必经此处；拦截必须带 layer+reason 留证。
- 关系：S3 validate_sql 节点、S4/S8 的 execute 工具、`writer.validate_write`（复用其 AST 表名提取与 CTE 排除）全部依赖它。
- 注意：必须 sqlparse AST 判断不能正则——注释/字符串里的关键字不算语句；CTE 别名要排除出表名集合（否则 `WITH t AS...` 被误杀，S8 评测 b3 实测踩中）；表位出现 Function 分组要接住（`INSERT INTO t(col)` 的坑，读写两路共用）。

**⑥ `src/safety/writer.py`（176 行）**
- 内容：写路径三道闸 `validate_write` + 事务 dry-run `dry_run_write` + 正式执行 `commit_write` + `audit_log`。
- 作用：S9 HITL 的安全内核——LLM 只能提案，人审后经独立写凭据执行。
- 关系：`server.build_write_proposal`（提案生成）、`server.api_confirm`（人审执行）调用它；表名提取复用 validator。
- 注意：dry-run 的灵魂是 ROLLBACK——`changes()` 必须在同事务内取才是精确值；`_rw_conn` 的 `row_factory=Row` 是取证样本 dict() 不炸的前提。

### 第 4 层 · Prompt 层（把知识喂给模型）

**⑦ `src/agent/prompting.py`（109 行）**
- 内容：`render_schema_block`（DDL+行内中文注释+采样行）/ `render_fewshots_block` / `build_system_prompt`（含 7 条业务口径铁律）。
- 作用：S2 消融证明的「Schema 注入 +0.4」就发生在这里——注释以 `--` 拼进 DDL，列名与解释零距离。
- 关系：S3 `nodes.generate_sql`、S4 `registry._tool_generate_sql`、S8 `_schema_block` 三处注入同源于此。
- 注意：口径规则（默认 delivered / BRL 不换算 / customer_unique_id 去重）必须与评测金标口径一致——这是评测设计踩坑的教训。

**⑧ `src/agent/fewshot.py`（83 行）**
- 内容：`FewShotRetriever`——bigram 词频 + 业务关键词加权余弦，Top-3 检索。
- 作用：给 prompt 注入相似历史示例（S2 消融 0.600→1.000 的另一半来源）。
- 关系：被 S3/S4 的 generate 路径调用；few-shot 库在 `evals/few_shots.json`（与金标集严格错题）。
- 注意：刻意不用 GPU embedding（SQL Agent 保持 CPU 可部署）——库上千条时换 BGE-M3，接口不变。

### 第 5 层 · S3 确定性管线（LangGraph 手写图）

**⑨ `src/agent/state.py`（35 行）**
- 内容：`SQLAgentState` TypedDict——messages / sql / sql_error / retry_count / token_cost / result / status 等字段及设计理由。
- 作用：S3/S4 共用的状态总线定义。
- 关系：react_graph 与 agentic_graph 的 StateGraph 都以它为 schema。
- 注意：字段的类级「默认值」仅是文档性书写，TypedDict 运行时不生效——实际读取统一 `state.get(key, 默认)`。

**⑩ `src/agent/nodes.py`（282 行）**
- 内容：S3 六节点实现——understand（口径规范）/ generate_sql（Schema+few-shot+错误回喂）/ validate_sql / execute_sql / self_correct（计数器）/ respond（ok/blocked/degraded 三终态）+ 两个路由函数。
- 作用：确定性管线的全部业务逻辑；`_account_usage` 是 S3/S4/S8 共用的 token 记账单一实现。
- 关系：由 react_graph 注册进图；依赖 prompting/fewshot/validator/llm。
- 注意：ReAct 循环的本质是**条件边把 execute 失败引回 generate**——图本身不循环；错误经 messages 回喂，模型看到上一轮报错就是 Observe 步；防死循环靠 MAX_RETRY + TOKEN_BUDGET 双保险。

**⑪ `src/agent/react_graph.py`（82 行）**
- 内容：`build_agent_graph()` 六节点连线 + `answer()` 入口封装。
- 作用：S3 图的组装层，add_node/add_conditional_edges/条件边映射表都在这里。
- 关系：nodes 提供节点函数；server 的 `_GRAPHS["react"]` 缓存其产物。
- 注意：和 agentic_graph 相比，「流程写死 vs 模型自主」的差别只在边和节点数。

### 第 6 层 · S4 工具循环（Function Calling 双保险）

**⑫ `src/tools/models.py`（121 行）**
- 内容：四工具的 Pydantic 入参模型（长度/类型/枚举/黑名单字段校验）+ `json_schema_to_pydantic`（S7 MCP 动态建模）。
- 作用：工具参数「第二道保险」——JSON Schema 是给模型的软约束，Pydantic 是服务端硬校验。
- 关系：registry.dispatch 在调用任何工具（原生或 MCP 外部）前都先实例化 PARAM_MODELS。
- 注意：外部工具不旁路——`json_schema_to_pydantic` 让 MCP 工具走同一条校验通道。

**⑬ `src/tools/registry.py`（260 行）**
- 内容：`TOOL_SCHEMAS`（给 LLM 的 OpenAI function schema）/ 四工具服务端实现 / `dispatch` 统一分发 / `register_external_tool`（S7 动态注册）。
- 作用：工具层唯一入口——agent 的 tools 节点只调 dispatch，不关心工具是原生还是 MCP。
- 关系：execute 工具内部再走 validator+execute_with_timeout（安全与编排解耦，双保险）；`_build_context` 与 nodes 的同名函数同构（S3 先有、S4 复制——已知重复，抽公共层列为改进项）。
- 注意：`_resolve_rows` 的 data_ref 容错（模型偶尔编造引用名）是真实评测喂出来的补丁。

**⑭ `src/agent/agentic_graph.py`（235 行）**
- 内容：agent 节点（bind_tools）/ tools 节点（dispatch→ToolMessage 回喂）/ respond + 循环路由 + `build_agentic_graph()`。
- 作用：S4 的「LLM 自主编排」——模型决定调什么工具调几轮，图只做调度与护栏。
- 关系：tools→registry.dispatch；token 记账复用 nodes._account_usage；MAX_STEPS/TOKEN_BUDGET 与 S3 同源同值。
- 注意：工具结果截断 `_OBSERVE_MAX_ROWS=30`（防上下文打爆）、同参数 tool_call 去重（AGNES 偶发重复调用）、安全拦截错误照样回喂（模型学会如实告知用户而不是硬闯）。

### 第 7 层 · S8 多智能体（黑板 + 质量门控）

**⑮ `src/agent/supervisor_graph.py`（501 行）**
- 内容：planner / sql_agent / analysis_agent / viz_agent / judge / respond 六节点 + `SupervisorState`（含黑板与 emit_* 瞬态字段）+ 双层门控 + `build_supervisor_graph()`。
- 作用：第三种编排——任务拆解、分工（子 Agent 对话隔离、黑板共享产出物）、质量裁决（Python 硬门控 + LLM-as-Judge，fix 打回重做 revision≤2）。
- 关系：sql_agent 内部复用 validator+execute_with_timeout；token 记账对齐 `_account_usage` 口径；emit_* 字段由 server 的 `_norm_supervisor` 转 SSE 事件。
- 注意：**路由函数不能改 state**——plan 推进的责任落在每个子 Agent 自己写 `step_index`（`_own_index`），否则原地死循环（S8 首跑实测踩中，GraphRecursionError）；子 Agent 上下文不能裁剪过头（漏 Schema 注入 → 列归属猜错，b2 教训）。

### 第 8 层 · 服务化与前端（把图变成产品）

**⑯ `src/api/server.py`（587 行）**
- 内容：全部 REST/SSE 接口（query/schema/history/feedback/confirm/upload_csv/health）+ 写意图旁路 + 三种图的事件归一化（_norm_agentic/_norm_react/_norm_supervisor）+ 生产者线程→队列→异步消费者的真流式。
- 作用：图的 HTTP 外壳——统一事件协议、多轮会话隔离、历史回放、HITL 审批端点。
- 关系：懒构建图缓存 `_GRAPHS`（首次请求才 import，启动不烧 token）；proposal 生命周期落 service.db。
- 注意：thread_id 必须每请求独立（固定 default 会把上一轮未收敛现场串进下一查询）；`_strip_chart_xml` 净化模型偶尔复读的图表配置。

**⑰ `web/index.html`（573 行）**
- 内容：免构建三栏玻璃拟态前端——Schema 树 / SSE 消费与轨迹渲染 / 结果表 + ECharts + 写提案审批卡 + CSV 上传。
- 作用：演示与走查入口；SSE 事件的最终消费者。
- 关系：事件协议与 server 第③步一一对应；`?graph=` 切换三种编排。
- 注意：SSE 帧解析必须按 `/\r?\n\r?\n/` 切（sse-starlette 发 CRLF）——「curl 看得到的流 ≠ 浏览器渲染得出来」的教训在 P1/P2 各踩过一次。

### 第 9 层 · MCP 与外围

**⑱ `src/mcp_server.py`（78 行）**：FastMCP 把 registry 四实现按 MCP 暴露（stdio + --http）；compute_metric/render_chart 的 data_ref 适配成无状态 rows 传参。协议外壳不改安全语义。

**⑲ `src/mcp_client.py`（167 行）**：`MCPToolBridge`（后台事件循环 + AsyncExitStack 常驻会话 + run_coroutine_threadsafe 同步桥）+ `enable_mcp_tools` 一行接入。注意 AsyncExitStack 必须持住上下文管理器引用，否则 GC 断连（实测踩坑）。

**⑳ `examples/mcp_demo_server.py`（29 行）**：第三方演示 server（now_utc 工具），用来验证「外部工具即插即用 + 不旁路校验」。

**㉑ `src/db/csvimport.py`（115 行）**：CSV→建表→插入→行数质检→动态注册的系统路径（DDL 受控生成不经 LLM；表名强制 csv_ 前缀清洗——外部输入不可信）。

### 附录 · 测试与评测

- `tests/`（**215 passed / 2 skipped**，收集 217，默认口径；干净克隆需先建库）：test_mcp（协议回环+凭据三道防线+启动期黑盒验收）/ test_safety（AST 攻击用例）/ test_writer（三道闸+dry-run）/ test_supervisor（FakeLLM 驱动全图）/ test_cost（成本计量）/ test_init_db（建库脚本）/ test_output_encoding（控制台编码闸门）/ test_bench_concurrency（压测脚本自身，35 条）。安全边界以这些用例为准。两条真 LLM 用例由 `RUN_SLOW=1` 控制，默认 skip。
- `evals/`：60 条金标 + 各 Stage 评测脚本与落盘结果（s5_eval_result / s8_dual_result / latency_cost_result），数字全部真实跑出。
- `scripts/run_api.py`（启动 8501）、`scripts/init_db.py` / `init_db_mysql.py`（数据入库）。

## 7. 术语表（与代码注释同口径）

| 术语 | 定义 | 落点 |
|---|---|---|
| 四层安全 | ①只读账号（mode=ro/agent_ro）②表名白名单（AST）③LIMIT 注入 ④progress_handler 超时 | connect / validator / validator / validator |
| 双保险 | JSON Schema 软约束 + Pydantic 硬校验 | registry.TOOL_SCHEMAS + tools/models.PARAM_MODELS |
| HITL 提案-人审 | 写操作：模型提案 → dry-run 预估 → 人审 → 独立写凭据执行 → 审计 | safety/writer + server /api/confirm |
| 事务 dry-run | BEGIN→写→changes()→ROLLBACK，零副作用拿精确影响行数+取证样本 | writer.dry_run_write |
| 黑板模式 | 子 Agent 对话隔离，只把产出物写进共享 blackboard，Judge 只看黑板 | supervisor_graph.SupervisorState.blackboard |
| LLM-as-Judge | 双层质量门控：Python 硬门控（完整性）+ LLM 软门控（溯源/口径），fix 打回 revision≤2 | supervisor_graph.judge |
| 错误回喂自愈 | 执行失败把错误写回 messages/重新 prompt，模型据此修正（ReAct Observe→Reason） | nodes.self_correct / supervisor sql_agent |
| 读写凭据分离 | 读走 mode=ro 连接，写走无 ro 的独立连接（MySQL 形态=agent_ro/agent_rw 双账号） | connect.get_conn / writer._rw_conn |
| 口径规则 | 默认 delivered / BRL 不换算 / customer_unique_id 去重 / 相对时间 2018 兜底 | scenarios/olist.py（全引擎取值） |
| 场景包 | 业务以 ScenarioProfile 数据类注入，图/安全/工具零改动可换场景 | src/scenarios/ + 各 prompt 消费点 |
| 事件归一化 | 三种图的节点更新 → 统一 6 类 SSE 事件协议 | server._norm_* |

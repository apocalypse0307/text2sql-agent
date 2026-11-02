# text2sql-agent — 面向业务取数的数据分析 Agent

[![CI](https://github.com/apocalypse0307/text2sql-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/apocalypse0307/text2sql-agent/actions/workflows/ci.yml)

> 自然语言 → SQL → 结果 → 图表的对话式取数系统。
> **手写 LangGraph 状态图编排**（禁用 `create_react_agent`），四层只读安全，
> 60 条金标评测集 + 自愈专项，FastAPI SSE 流式 + 玻璃拟态三栏前端。

![Python](https://img.shields.io/badge/Python-3.12-blue) ![LangGraph](https://img.shields.io/badge/LangGraph-handwritten%20StateGraph-teal) ![License](https://img.shields.io/badge/license-MIT-green)

## 它解决什么问题

不会 SQL 的业务人员（运营/产品/管理者）用自然语言直接拿数字，
把「提需求 → 数据组排期 → 等回数」的长链路压缩成 30 秒问答。
业务口径（默认 delivered、客户去重用 customer_unique_id、金额含运费）写死在
system prompt，保证人人问出一致口径。

## 三种编排对照（核心设计）

| | react_graph（S3） | agentic_graph（S4） | supervisor_graph（S8） |
|---|---|---|---|
| 编排哲学 | **确定性管线**：understand→generate→validate→execute 顺序由代码写死 | **LLM 自主编排**：模型每轮输出 tool_calls，图只做「执行→观察→回喂」循环 | **规划-分工-裁决**：Planner 拆解 → 三个子 Agent（黑板共享、messages 隔离）→ Judge 质量门控 |
| 模型角色 | 只负责 generate 一步 | 自主决定调什么工具、调几轮 | 子 Agent 各司其职；Judge 有否决权（fix 打回重做，revision≤2） |
| 适合场景 | 流程固定、可解释性优先 | 开放任务、工具组合多样 | 高质量要求、可接受额外调用成本 |
| 共同护栏 | MAX_STEPS + TOKEN_BUDGET + 四层安全（与编排无关，双保险） | 同左 | 同左 + judge 两层门控（Python 硬门控 + LLM-as-Judge） |

三者都手写 `add_node` / `add_conditional_edges`（禁 prebuilt）。S3/S4 同一套行为用例 8/8 双过。

**S8 双跑对照（DeepSeek-V4.1-Flash，2026-10-16）**：两种编排**都 8/8 全过**（AGNES 时代 supervisor
只有 7/8，唯一失败来自免费额度 429）。真实代价是 **延迟 +56%、成本 +62%**
（agentic ¥0.0278 / supervisor ¥0.0451）——见下方「计量订正」。

> ★ **计量订正**：早期版本写「supervisor token −56%」，那是拿**两个口径不一致的
> 数**相除来的。图内 `state.token_cost` 对 agentic **少算 31.4%**（47,870 vs API 实测 69,855）——
> 根因是 agentic 通过 `bind_tools` 暴露的 `generate_sql` **工具内部自己也调了一次 LLM**
> （`tools/registry.py:111`），**嵌套调用的 token 没进 state**。改用同一口径（直接读 API usage 的
> `CostTracker`）重算：**−9.8%**，不是 −56%。**这个不一致是靠"加一个独立计量"才发现的**——
> 单一计量口径无法自证。详见 `METRICS.md`。
前端「Supervisor 多智能体」按钮可现场演示判官打回循环。

![Supervisor 多智能体](assets/supervisor-judge-loop.png)

> 上图：顶部编排切换按钮可见当前处于 **Supervisor 多智能体**。结论卡里的「数据覆盖完整 / 去重处理 /
> 结果有效性」三项是子 Agent 分工产出后经判官门控通过的内容——**口径自检写进回答本身**，
> 而不是让用户自己去核对（业务取数最怕的是「数字对了但口径错了」）。

## 实测指标（全部真实跑出，详见 METRICS.md）

| 指标 | 数字 | 说明 |
|---|---|---|
| Few-shot 消融 | 0.600 → **1.000**（+0.4） | Schema 注入 vs +Top-3 few-shot，10 条金标 |
| SQL 生成准确率 | **0.860**（43/50） | 60 条全量评测（6 条安全题除外），AGNES 基线 |
| 端到端结果匹配 | **0.870**（47/54） | 同上 |
| 自愈成功率 | **0.708**（17/24） | 注入 3 类真实执行错误后恢复；**DeepSeek 复跑同为 0.708** |
| 安全拦截 | **6/6** | 恶意请求全拦，无写操作落地 |
| 跨模型复跑 | SQL 0.780 / 端到端 0.796 | DeepSeek-V4.1-Flash 全量 60 条，**实测成本 ¥0.1755、耗时 460s（快 5.8×）**；差异逐条归因见 METRICS（**真实模型差异 0 条**） |
| 成本计量单测 | 24/24 | `tests/test_cost.py`：计价算术 / 缓存命中四种键名 / 峰谷分档 / 熔断；**已做变异验证非空转** |
| 攻击用例 | 17/17 | AST 层单测（越权表/子查询绕过/拼接/OUTFILE） |
| 写路径安全用例 | 18/18 | S9 HITL：三道闸 8 + dry-run 零副作用 3 + commit/审计 2 + CSV 导入 5 |
| Supervisor 离线用例 | 18/18 | S8：FakeLLM 驱动全图（happy path / 判官打回循环 / 安全降级 / 规划纠偏 / analysis 与 viz 并行 / 黑板写入权限） |
| CTE 白名单用例 | 4/4 | S8 连带修复：WITH 别名不被误杀、不掩盖真越权表 |
| MCP 协议用例 | **35/35**（`RUN_SLOW=1`）/ 34 + 1 skip（默认） | S7：stdio/http 双传输回环 + 安全不旁路 + 第三方工具动态注册 + **子进程凭据白名单 7 条** + **凭据失败形态 11 条（含接线与反向对照）** + **启动期黑盒验收 3 条 + 入口不可绕过 2 条** |
| 建库脚本用例 | 11/11 | `tests/test_init_db.py`：缺 CSV 时点名文件 + 指路 Kaggle（而非裸 `FileNotFoundError`） |
| 服务端导入用例 | 4/4 | `tests/test_server_import.py`：服务自己创建数据目录（镜像首次启动曾因 `sqlite3.connect()` 不建父目录而 `OperationalError`） |
| 控制台编码闸门 | 6/6 | `tests/test_output_encoding.py`：AST 扫描 print/raise/logging/exit，禁 emoji 与 `¥`(U+00A5)——cp936 编不了，`PYTHONIOENCODING=gbk` 下会崩 |
| 压测脚本自身用例 | 35/35 | `tests/test_bench_concurrency.py`：SSE 帧解析（P1 命名事件 / P2 纯 data 两种）、QPS/分位数计算、并发串场判定（**压测脚本也要测**） |
| 服务层测试 | pytest **215 passed / 2 skipped**（默认口径；收集 217）<br>`RUN_SLOW=1` + 配好凭据 → **217 条全跑、0 skipped** | 工具双保险 + 安全层 + S9 写路径 + S8 Supervisor + S7 MCP + 场景包 + 成本计量 + 建库脚本 + 服务端导入 + 编码闸门 + 压测脚本 + 多轮状态 + 服务端行为 |

**为什么会有 skip、以及为什么这个数必须可复现**：只有两条真实 LLM 用例会跳过
（`tests/test_tools.py` 与 `tests/test_mcp.py` 各一条），它们由 `RUN_SLOW=1` **显式开关**控制，
而不是「看谁机器上碰巧有 API key」——否则同一个仓库在别人机器上是 215/2、配了 key 的机器上是 217/0，
写在文档里的通过数就说不清。现在的口径是：**默认（不带任何环境变量、不配任何 key）＝ 215 passed / 2 skipped，
任何机器一致**；要跑全量就 `RUN_SLOW=1`，且需配好凭据（未配则给一条点名变量的 skip，而不是红）。
其余用例**零外部依赖**：不联网、不读凭据、不需要人工准备数据。

评测卫生：金标集与 few-shot 库严格错题；金标口径与 system prompt 一致（踩过 3 个评测设计坑）。
性能账本（真实计时/计费，详见 METRICS.md）：S5 全量延迟 mean 44.3s / median 37.0s / p95 101s，
**自愈的延迟代价 = 一次通过 +25%**（50.9s vs 40.7s）。换 DeepSeek-V4.1-Flash 全量复跑：
S5 端到端 **460s vs AGNES 约 2658s（快 5.8×）**、三个评测合计 **约 ¥0.29**（实测，非估算）。

## 库的增删改查（S9：HITL 提案-人审）

读路径让 Agent 自主循环；**写路径永不进自主循环**——模型只有建议权，人有否决权与执行权：

1. 写意图（正则粗筛 + LLM 严格 JSON 判定）→ 生成**提案 SQL**（仅 INSERT/UPDATE/DELETE、表白名单、UPDATE/DELETE 必带 WHERE）
2. **事务 dry-run**：BEGIN→执行→SELECT changes()→ROLLBACK，零副作用拿到精确影响行数 + 「将被改动的行」取证样本——dry-run 还能提前抓住约束违约（如 NOT NULL），拦截后转读路径兜底
3. SSE 推 `proposal` 事件 → 前端审批卡（SQL / 影响行数 / 取证样本 / 风险说明）→ 人点「确认执行 / 拒绝」
4. `/api/confirm` 复检三道闸 → **独立写凭据**（SQLite 去 mode=ro；MySQL 形态 agent_rw 账号）事务执行 → `write_audit.jsonl` 审计留痕

数据接入：左栏「＋ 导入CSV」→ 表名/列名清洗 + 类型推断 + 行数质检 → **动态注册进白名单与 Schema 注入**（重启自动恢复），Agent 下一句即可查询新表。

![写路径 HITL](assets/write-proposal-hitl.png)

> 上图一次演示里同时可见两个状态：**提案未执行**（INSERT 卡 + 事务 dry-run 算出的「预估影响 1 行」
> + 目标表白名单校验 + 风险说明）与**人审通过后**（「已执行·实际影响 1 行（审计已记录）」）。
> 左栏底部的 `csv_region_targets` 就是 CSV 导入后**动态注册**的新表。

## MCP 对外开放 + 第三方工具接入（S7）

能力开放遵循一条原则：**MCP 只是协议外壳，安全语义不变**——

- **Server**（`src/mcp_server.py`，FastMCP）：把 generate_sql / execute_readonly_sql /
  compute_metric / render_chart 四个工具按 MCP 暴露，实现层复用 `src/tools/registry`
  同一份代码（不是绕过安全的副本）。支持 stdio 与 streamable-http 双传输：
  `python src/mcp_server.py --http 8600`
- **Server 启动期体检**：`mcp.run()` 之前先 `require_llm_credentials()`。
  缺凭据默认**硬失败**（stderr 点名变量 + 三步修法 → `exit 1`），
  `--allow-degraded` 则只打 WARN 并**不注册 generate_sql**（其余 3 个工具不需要 LLM，照常可用）。
  ★ stdio 模式下 **stdout 是协议信道**，所有诊断一律走 stderr——有测试逐行断言 stdout 全是合法 JSON-RPC 帧。
- **Client**（`src/mcp_client.py`）：后台事件循环 + AsyncExitStack 保活连接；
  任何 MCP server 一行接入：
  ```python
  from src.mcp_client import enable_mcp_tools
  enable_mcp_tools([{"name": "ext", "transport": "http", "url": "http://127.0.0.1:8610/mcp"}])
  # 发现 → inputSchema 动态转 Pydantic → register_external_tool → Agent 下一轮可见

  # 接本仓库自己的 server 时请显式声明 requires_llm：
  # 它带启动期体检，缺凭据会 exit(1)，客户端闸门必须抢在**连接前**判定，否则只能拿到
  # 一条协议层「连接失败」，看不出是凭据问题
  enable_mcp_tools([{"name": "p2", "transport": "stdio",
                     "command": sys.executable, "args": ["src/mcp_server.py"],
                     "requires_llm": True}])
  ```
- 经 MCP 调 `DROP TABLE` / 查白名单外表**照样被四层安全拦截**；
  与原生同名的第三方工具被跳过（原生优先），新工具经 Pydantic 校验通道 dispatch（无旁路）。

**子进程凭据：不做全量环境透传，走显式白名单，且**取值源是 `Settings`**（`src/mcp_client.py::_MCP_ENV_FROM_SETTINGS`）

stdio 子进程的环境**不是**父进程 `os.environ` 的复制——MCP SDK 只透传
`get_default_environment()` 的白名单（本机实测仅 12 个变量：`PATH` / `APPDATA` / `TEMP` /
`USERNAME` …，**零凭据**）。于是「父进程明明配好了 `DEEPSEEK_API_KEY`，子进程却报
`Missing credentials`」——根因在传参层，不在密钥配置层。

修法刻意**不做 `**os.environ` 全量透传**：那等于把父进程所有密钥（数据库口令、云厂商
token、第三方 key）一并塞进一个我们并未逐行审计过的子进程，是用「修好了」换「泄露面无限大」。
正确姿势是**显式白名单**——子进程要自建 LLM 与只读连库，就只给它这两类凭据；写库口令
`agent_rw` 刻意不在名单内。

白名单的值**一律从 `Settings` 读，不从 `os.environ` 读**：pydantic-settings 会把 `.env`
与进程环境合并，只读 `os.environ` 会漏掉**只写在 `.env` 里**的配置（此前 AGNES 能跑纯属
子进程自己也读了 CWD 的 `.env` 兜底——同一处兜底把同一个缺陷掩盖了两次）。改从 settings 取
还顺带得到两点：子进程**不再依赖 CWD 能找到 `.env`**（从任意目录起 `mcp_server.py` 都能跑）；
配置来源唯一，测试可直接 `monkeypatch` 伪造 settings 字段，**不必改真实环境变量**，
红绿不再取决于跑测试的机器。

`tests/test_mcp.py` 有 **7 条零 LLM 消耗**的用例钉住这条边界（其中`test_child_env_takes_value_from_settings_not_environ`
用**冲突值**做决定性断言：`os.environ` 与 `settings` 给不同的 key，子进程必须拿到 settings 那份）。
两次变异验证非空转——回退成读 `os.environ` → 4 红；白名单里塞一个不存在的字段名 → 字段存在性检查当场报错。

**凭据缺失怎么失败：三道防线，越早越好**

`list_tools` 本身不调 LLM，所以凭据缺失时**连接与注册都会成功**，失败曾经是**惰性**的——
等第一次真正调 `generate_sql` 才炸，还被上层兜成 `degraded`。用户看到的是「Agent 好像不太会
写 SQL」，而不是「你少配了一个变量」。现在修成三层：

- **① 服务端启动期**（`src/mcp_server.py`）：`mcp.run()` 之前先 `require_llm_credentials()`，
  缺凭据默认 `exit(1)`（stderr 写「缺哪个变量 / 写 `.env` 还是 `setx` / 怎么验证」），
  `--allow-degraded` 则 WARN + 不注册 `generate_sql`。这一层连客户端都不需要存在。
- **② 客户端闸门**（`enable_mcp_tools`）：看情况分两个时机——
  `spec["requires_llm"]=True` 时**连接前**判定（否则子进程先 `exit(1)`，我们连 `list_tools` 都走不到，
  只能等到一条协议层「连接失败」）；未声明则**连接后**按工具集自动判定。
  默认 `strict=True` 抛 `MissingCredentialError`；`strict=False` 改为**打 WARNING 并跳过该 server 的
  工具注册**——这是「不暴露工具」唯一合法的用法：显式降级、且留了声音。
- **③ 调用期**（`_tool_generate_sql`）：把 SDK 原话（`openai.OpenAIError: Missing credentials.
  Please pass an api_key … or set OPENAI_API_KEY`）翻译成
  `{"ok": false, "error_code": "MISSING_CREDENTIALS", "retryable": false, "error": "缺少 LLM 凭据：…DEEPSEEK_API_KEY…"}`。
  两个理由：**SDK 指向的 `OPENAI_API_KEY` 我们根本不用**，报错必须点名真实变量；
  ToolMessage 是**喂给模型**的，`retryable: false` 明确叫它别重试，否则它会当成「工具坏了，换一个试试」白烧步数。

判据按**工具集**而不是按 server 名：`execute_readonly_sql` / `compute_metric` / `render_chart`
都不建 LLM 客户端，只有 `generate_sql` 会——否则第三方天气/日历 server 会因为**我们自己没配
LLM key** 被误拦。这条边界有 17 条零 LLM 用例 + 7 次变异验证守着（去掉结构化错误 → 1 红；闸门失效 → 2 红；
判定一律「需要凭据」→ 2 红；删掉主流程里的闸门调用 → 2 红而 9 条叶子用例全绿；
跳过启动期体检 → 2 红；失败文案打到 stdout → 1 红；白名单漏项 → 夹具阶段红，绝不 skip）。
另有两场景实测：有凭据 33 passed；无凭据 32 passed / 1 skipped（只有真 LLM 那条跳过）。

## 业务外壳可插拔：场景包（Scenario Pack）

引擎与业务口径**解耦**——把「随业务变化的口径级参数」收进一个不可变数据类，换场景不改图/安全/工具一行代码。

| | |
|---|---|
| 定义 | `src/scenarios/base.py::ScenarioProfile`（frozen dataclass，10 个字段） |
| 内置 | `src/scenarios/olist.py::OLIST`（电商 / SQLite / 数据范围 2016-09~2018-10 / 默认状态 `delivered` / 去重键 `customer_unique_id` / 币种 BRL / 业务表枚举） |
| 消费点 | **5 处**：`agent/nodes.py`、`agentic_graph.py`、`supervisor_graph.py`、`agent/prompting.py`、`api/server.py` |
| 契约测试 | `tests/test_scenario.py`（6 用例，含**医疗场景换装演示**：换成 patient 表 + 挂号状态口径，断言 prompt 随之变化） |

**字段取舍原则**：只收「多个 prompt 共用、且随业务变化」的值；单处使用或纯模板措辞留在各 prompt 里——避免过度抽象。

**为什么值得做**：口径散落在 5 个 prompt 里时，「改一个业务口径」= 改 5 个地方且**必然漏改**；
而漏改的后果在评测上是**区分度丢失**——金标口径与 prompt 口径不一致时，模型「遵守规则」却被判错
（S5 踩过这个坑）。收进 frozen dataclass 后改口径只改一个文件，并由测试钉住
「改 profile → 全部 prompt 同步变化」这条性质。

## 架构

```
用户问题
   │
   ▼
FastAPI /api/query (SSE)                     ┌────────────────────────┐
   │  producer thread + queue                │ react_graph（确定性管线）│
   ▼                                        │   understand → generate │
┌──────────────┐    ┌──────────────────┐    │   → validate → execute  │
│ 四层安全层    │───▶│ Olist 9 表        │    │   → self_correct ↺     │
│ 只读/白名单/  │    │ SQLite dev 库     │◀───┤                        │
│ LIMIT/超时   │    │ (MySQL 就绪:      │    │ agentic_graph（LLM 自主）│
└──────────────┘    │  agent_ro 只读)   │    │   agent ⇄ tools 循环    │
                    └──────────────────┘    │   → respond             │
                                            └────────────────────────┘
   事件: thought / sql / result / chart / answer / error
   ▼
三栏前端：Schema 树 │ 对话（SQL 折叠/反馈）│ 结果表 + ECharts
```

## 快速开始

> **需要准备两样外部资源**：① Olist 数据集（9 个 CSV，约 150 MB）；② 一个 LLM API Key。
> 两者都不在仓库里（数据涉版权、key 涉安全），下面给**可直接复制执行**的步骤。

### 1. 装依赖

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows；Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 下载 Olist 数据集 → 解压到 `data/raw/`

数据集：**[Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)**
（Kaggle 免费，需登录账号，下载后得到 `archive.zip`）。

解压后把内层 9 个 CSV 放到 `data/raw/`（文件名必须完全一致）：

```
data/raw/
├── olist_customers_dataset.csv
├── olist_geolocation_dataset.csv          # 或放原 zip 也行，init_db.py 会自动解压
├── olist_order_items_dataset.csv
├── olist_order_payments_dataset.csv
├── olist_order_reviews_dataset.csv
├── olist_orders_dataset.csv
├── olist_products_dataset.csv
├── olist_sellers_dataset.csv
└── product_category_name_translation.csv
```

> 官方 `olist_geolocation_dataset.csv` 有 100 万行 / 58 MB，是最大的一个。
> **9 个 CSV 必须放全**——`init_db.py` 建库前会先跑一次前置检查，缺哪几个就一次列全
> （附 Kaggle 链接与 `data/raw/` 路径），然后以退出码 `2` 结束。
> 不会「读一张崩一张」，也**不会先删旧库再崩**。

### 3. 建库

```bash
python scripts/init_db.py
# 建完自动刷新 data/schema.md（给人读的 Schema 文档，随仓库提交）
# 只刷新文档不重建库：python scripts/init_db.py --dump-schema
```

导入 9 表共约 155 万行，**首次约 15 秒**（实测 13.3s）。脚本会逐表打印行数并与官方口径校验
（对不上会打 ⚠️ 而不是静默通过）。产出 `data/db/olist.db`（约 130 MB，已 gitignore）。

### 4. 配 LLM（`.env`）

```bash
cp .env.example .env
```

```ini
# DeepSeek（本项目实测用的就是它）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
LLM_MODEL=deepseek-flash

# 或 Agnes 免费额度：LLM_PROVIDER=agnes / AGNES_API_KEY=...
```

> 数据库默认落 SQLite（`data/db/olist.db`），零配置。切 MySQL 只改 `DB_URL` 一行，
> 见 `.env.example` 里的说明（生产形态用 `agent_ro` 只读账号）。

### 5. 起服务

```bash
python scripts/run_api.py          # http://127.0.0.1:8501
```

三栏界面：左 Schema 树 / 中对话（SQL 可折叠、反馈）/ 右结果表 + ECharts。
试一句：**「2017年各品类销量 Top5，用图表展示」**。

### 6. Docker 一键启动（可选，推荐给「只想跑起来」的人）

```bash
docker compose up -d --wait          # api:8501；数据走 ./data 卷，镜像里不带库
curl http://127.0.0.1:8501/api/health
open http://127.0.0.1:8501/          # 三栏界面
```

首次用容器，先让容器把合成库建到卷里（真数据集请按第 2、3 步在宿主机建好再挂进去）：

```bash
docker compose run --rm --no-deps api python scripts/make_fixture_db.py
```

| 命令 | 作用 |
|---|---|
| `docker compose up -d --wait` | 起 api 并**等到 healthcheck 通过**才返回 |
| `docker compose down` | 停服务（`./data` 里的库与审计日志保留） |
| `docker compose --profile mysql up -d mysql` | 额外起 MySQL 8.4（默认 up 不含它） |

> **一个刻意的设计**：compose 里**不转发 `DB_URL`**。compose 会自动读项目根的 `.env`
> 做变量插值，而 `.env` 里通常写着 `DB_URL=sqlite:///D:/...`——那是宿主机 Windows 路径，
> 一旦塞进 Linux 容器，症状是「服务起来了、`docker ps` 正常，但一查询就
> unable to open database file」。所以让容器按 `PROJECT_ROOT=/app` 自己拼默认路径，
> 要切 MySQL 就在命令行上显式 `-e DB_URL=...`。
>
> **容器的两个必要条件**：`API_HOST=0.0.0.0`（`run_api.py` 默认 127.0.0.1 是给本机开发用的，
> 容器里留着它宿主机就连不上），以及服务自己会 `mkdir` 它的数据目录——
> 后者是真踩过的坑：`server.py` 导入期就落 `data/db/service.db`，而
> `sqlite3.connect()` 不会创建缺失的父目录，容器首次启动时 `data/` 不存在就
> `OperationalError` 直接起不来（本地永远复现不了，因为本地 `data/db/` 早被真实库占着）。

### 7. 自检（不耗 LLM 额度）

```bash
python -m pytest -q                # 默认 215 passed / 2 skipped（收集 217）
python evals/run_eval_s5.py        # 60 条全量评测（需 LLM，约 ¥0.18）
python evals/run_selfheal_eval.py  # 自愈专项（需 LLM，约 ¥0.04）
```

> **为什么会有 2 个 skip**：只有两条用例真调 LLM，由 `RUN_SLOW=1` 显式控制。
> 默认口径不联网、不读凭据。
>
> **但默认口径**并非「不需准备数据」**——这一点之前写错了，已订正**：
> 有 10 条用例要碰 `data/db/olist.db`（135 MB，由 Kaggle 数据集建出，`.gitignore` 排除）。
> 所以准确说法是「任何**建过库**的机器上 215 / 2」。干净克隆实测是
> **205 passed / 10 failed / 2 skipped**，10 条全因缺库。
>
> CI 与干净克隆上用合成库补齐（表结构真实、行数合成，够跑通代码路径）：
>
> ```bash
> python scripts/make_fixture_db.py     # 生成 data/db/olist.db（合成，约 22 MB / 2.4s）
> python -m pytest -q                   # → 215 passed / 2 skipped
> ```
>
> 真实业务基数由 `init_db.py` 的 `EXPECTED` 行数校验与 METRICS.md 负责，**不由单测负责**
> ——单测该钉不变量，不该钉数据集基数（`test_safety.py` 里写死 `== 99441` 的那条已改）。

### 8. 持续集成（GitHub Actions）

[`ci.yml`](.github/workflows/ci.yml)：`ruff`（真实缺陷类规则）→ 生成合成 fixture 库 → `pytest`。

[`docker.yml`](.github/workflows/docker.yml)：`--target test` 构建 → 容器内 pytest；
以及 `docker compose up -d --wait` → `/api/health` → `/api/schema`（9 张表）→
静态首页 → 容器内只读查一行 → `down`。两者都是绿的。

> 为什么 Docker 验证必须放 CI：开发机是 Windows + **没装 Docker**
> （`docker: command not found`，`wsl.exe` 也被安全策略拉黑），本地无从验证。
>
> `docker.yml` 首次真跑**红过一轮**，两个失败同一个根因：`server.py` 导入期落
> `data/db/service.db` 而目录不存在 → 容器里服务起不来。详见 METRICS.md「T2」。
推 main / PR 触发，约 4 分钟。

**为什么要单独生成 fixture 库**：库文件不入库，CI 是干净环境。上面那 10 条失败的用例
正是被 CI 的前置侦察（干净克隆）照出来的——CI 的价值在它建立之前就已经兑现过一次。

本地复刻 CI：

```bash
python -m venv /tmp/ci && /tmp/ci/bin/pip install -r requirements.txt ruff
/tmp/ci/bin/python scripts/make_fixture_db.py && /tmp/ci/bin/python -m pytest -q
```

> **别在本地随便跑 `make_fixture_db.py`**：它会写 `data/db/olist.db`，
> 覆盖掉你的真实库（脚本默认拒绝覆盖，除非加 `--force`）。

### 常见卡点

| 现象 | 原因 / 处理 |
|---|---|
| 建库报「缺少 N 个数据集文件」（退出码 2） | 第 2 步没做或文件名不对；报错里直接给了 Kaggle 链接和 `data/raw/` 路径，缺几个列几个 |
| 启动报「未读取到 DEEPSEEK_API_KEY」（退出码 1） | `.env` 没配或 `LLM_PROVIDER` 拼错；报错文案会点名变量 |
| 行数校验出现 `[MISMATCH]` | 数据集版本不同或 CSV 被截断，对照官方行数核对 |
| 脚本输出乱码或 `UnicodeEncodeError: 'gbk' codec can't encode` | Windows 控制台是 cp936(GBK)——能编中文，编不了 emoji 和 `¥`(U+00A5)。代码里统一用 `[OK]`/`[WARN]`/`[FAIL]` 和 `￥`(U+FFE5)，`tests/test_output_encoding.py` 的 AST 闸门守着；若仍撞见，跑该测试会点名到 file:line |
| 429 / 限流 | 免费额度常见；换 DeepSeek 或降低并发 |

## 代码结构

> 详细版（每文件的内容/作用/模块关系/注意事项 + 整体 pipeline 图 + 术语表）见
> **[ARCHITECTURE.md](ARCHITECTURE.md)**；下表是速览版。

| # | 文件 | 要点 |
|---|---|---|
| 1 | `src/core/config.py` | pydantic-settings；SQLite/MySQL URL 抽象（库无关设计） |
| 2 | `src/db/connect.py` | SQLAlchemy 双引擎；SQLite `mode=ro` creator 注入（只读兜底） |
| 3 | `src/safety/validator.py` | **四层安全**：sqlparse AST 白名单 / LIMIT 注入 / progress_handler 超时 |
| 4 | `src/agent/state.py` | LangGraph State 定义（Annotated reducer） |
| 5 | `src/agent/react_graph.py` | **手写确定性状态图**（对照 prebuilt 的差异） |
| 6 | `src/agent/agentic_graph.py` | **手写 ReAct 工具循环**：bind_tools → 条件边 → 观察回喂 |
| 7 | `src/tools/registry.py` | Function Calling 工具注册：JSON Schema（软约束）+ Pydantic（硬校验）双保险 |
| 8 | `src/agent/prompting.py` + `fewshot.py` | Schema 注入 / few-shot 加权检索（bigram TF，CPU 可部署取舍） |
| 9 | `src/api/server.py` | FastAPI + SSE 真流式（producer thread + queue）；事件归一化；输出净化 |
| 10 | `web/index.html` | 原生 JS 三栏布局；SSE 消费；markdown 逐行渲染；ECharts |
| 11 | `src/mcp_server.py` + `src/mcp_client.py` | **MCP 双向**：FastMCP 工具开放（复用 registry）+ AsyncExitStack 客户端 + 动态注册 |
| 12 | `src/scenarios/base.py` + `olist.py` | **场景包**：口径级参数收进 frozen dataclass，5 个消费点零改动换业务外壳 |
| 13 | `evals/run_eval_s5.py` | 评测框架：金标比对 / 断点续跑 / 结果归因 |
| 14 | `METRICS.md` | 全部指标数字与踩坑记录 |

## 已知边界与生产化路线（诚实清单）

数据层当前为开发期一次性导入的静态快照；生产落地还需：
企业数据链路（ETL/CDC → 分析从库，Agent 只当读者）、SSO/RBAC 行列权限与审计、
PII 脱敏、schema 变更感知与 schema linking、并发/缓存/限流、Docker 部署。

**写路径的「提案-人审-执行」已实现**（S9，走 SSE `proposal` 事件 + `/api/confirm` 复检三道闸），
剩余的是更成熟的形态：用 LangGraph `interrupt` 支持**可中断/可恢复**的长事务审批、
并发场景下的提案状态管理（当前提案活在单次请求的生命周期内）。
详见 METRICS.md。

**已知记账缺陷（未修，已定位）**：agentic 图通过 `bind_tools` 暴露的 `generate_sql` 工具
内部自己也调一次 LLM（`tools/registry.py:111`），**这类嵌套调用的 token 不会进 `state.token_cost`**
（实测少算 31.4%）。当前以 CostTracker（读 API usage）为跨编排比较的权威口径绕开了它；
若要根治，应让工具复用节点实现并把嵌套用量回写 state——同时也能顺带消掉「工具版 SQL 生成
与节点版提示词构建的重复」。

## License

MIT

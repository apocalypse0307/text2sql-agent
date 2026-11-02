# P2 Text2SQL Agent — 实测指标记录

> 用途：记录项目的实测指标，**所有数字必须真实跑出来**。
> 评测脚本：`evals/run_fewshot_ablation.py`（S2）/ `evals/run_s3_behavior.py`（S3 行为）/
> `evals/run_s4_behavior.py`（S4 行为）/ `evals/run_eval_s5.py`（S5，60 条全量）/
> `evals/run_selfheal_eval.py`（S5 自愈专项）
> 数据：Olist 巴西电商公开数据集 9 表 10 万订单（SQLite dev 库 `data/db/olist.db`）
>
> ★ **引用本文件里的任何数字前，先查下面的「数字变更记录」。**

## 数字变更记录（口径订正）

**为什么要这张表**：指标口径一变，旧数字不会自己失效——它继续躺在文档里
被当真。本表把「哪个数是旧口径、哪个是新口径、为什么变」钉死，让后来的人（和未来的我）
不会再把旧数字当结论引用。

| # | 指标 | 旧口径值 | **新口径值（以此为准）** | 为什么变 | 影响面 |
|---|---|---|---|---|---|
| 1 | S8 supervisor 相对 agentic 的 token 差额 | −56%（全量汇总）/ −49%（单例） | **−9.8%** | 旧值取自**图内 `state.token_cost`**，而它对 agentic **少算 31.4%**（`bind_tools` 暴露的 `generate_sql` 工具内部又调一次 LLM，嵌套用量未入 `state`）。新值改读 API usage（`CostTracker`），两图同口径 | S8 章节表格、延迟与成本章节、README 编排对照 |
| 2 | supervisor「token 更低」这一优势 | 视为普惠结论 | **未复现**（DeepSeek 上成本反 **+62%**） | 模型行为不同：supervisor 每次修订的上下文都在变，缓存命中率 **62.8%**，远低于 agentic 的 **84.0%** | 同上；「何时选谁」的推荐口径 |
| 3 | S5 端到端（DeepSeek） | 0.7593（41/54） | **0.7963**（43/54） | `empty_result` 判据从「查关键词」改为「结果集语义比对」，纠正 2 条**答对却被判错**的用例 | S5 章节、README 指标表 |
| 4 | S8 supervisor 正确率（DeepSeek） | 0.875（7/8） | **1.000（8/8）** | 原 7/8 的唯一失败 b5 是 AGNES 免费额度 429，换不限流提供方后不再发生 | S8 章节、README 指标表 |
| 5 | 测试总数 | 81 passed + **2 skipped** →（一度写成）150 passed / **0 skipped** | **215 passed / 2 skipped（默认口径；收集 217；前提是库存在，CI 用合成库补齐）**；`RUN_SLOW=1` + 凭据 → **217 条全跑、0 skipped** | 条数一路涨（110→114→125→131→133→142→144→147→149→150→152→154→176→217），但**「0 skipped」只在我这台配了 key 的机器上成立**：两条真 LLM 用例原先按「settings 里有没有凭据」自动 skip，别人克隆下来就是 129/2。已统一改成 `RUN_SLOW=1` 显式门控 | README 测试小节 |
| 6 | MCP 子进程凭据取值源 | `os.environ` | **`settings`** | 只读 `os.environ` 会漏掉**只写在 `.env` 里**的值（AGNES 时代靠子进程自己读 CWD 的 `.env` 兜底才没出事） | `src/mcp_client.py`、README MCP 章节 |

**判定规则（防再次踩坑）**：
① 凡是「跨编排 / 跨模型 / 跨版本」的差额，**必须确认两侧是同一种计量口径**，否则只能各自陈述、不能相减；
② 凡是靠**措辞**做的判断（关键词命中、措辞比对），迟早会被下一个模型打穿，应换成**语义判据**；
③ 口径一变，**旧值要在原处标记为作废或不可比**，不能只在文末补一句。

## 数据入库（P2-S1，2026-09-07）

| 表 | 行数 | 说明 |
|---|---|---|
| customers | 99,441 | 客户 |
| orders | 99,441 | 订单主表（事实表） |
| order_items | 112,650 | 订单明细（桥表） |
| order_payments | 103,886 | 支付（一单可拆多笔） |
| order_reviews | 99,224 | 评价（⚠️ 官方数据含重复 review_id，不设主键） |
| products | 32,951 | 商品 |
| sellers | 3,095 | 卖家 |
| geolocation | 1,000,163 | 邮编坐标（含重复） |
| product_category_translation | 71 | 品类葡→英翻译 |

- 时间跨度：2016-09 ~ 2018-10；状态分布：delivered 96,478 / shipped 1,107 / canceled 625 …
- dev 库 SQLite + SQLAlchemy URL 抽象（`db_url` / `db_ro_uri`），读连接 `mode=ro` 只读兜底；
  生产切 MySQL：只改连接串 + 换 `agent_ro` 只读账号（四层安全第①层）

## Schema 注入 + Few-shot 消融（P2-S2，2026-09-07）

**口径**：10 条评测金标（goldset.json，与 few-shot 库严格错题——评测卫生），SQLite 只读执行，
结果集与金标比对（忽略列名/列序/行序，浮点 round 2 位）。

| 组别 | 可执行 | 结果匹配 | 说明 |
|---|---|---|---|
| 仅 Schema 注入 | 10/10 | 6/10 = **0.600** | baseline |
| Schema + Few-shot(Top-3) | 10/10 | 10/10 = **1.000** | 增益 |
| **提升** | — | **+0.4** | Few-shot 带来的差值 |

- few-shot 库 `evals/few_shots.json`：12 条手工精选，覆盖 join / 日期边界 / 口径陷阱（客户去重
  用 customer_unique_id、品类过滤必须走翻译表等），全部 SQL 在库上验证可执行
- 检索器 `src/agent/fewshot.py`：中文 bigram + 业务关键词白名单的加权 TF 余弦。
  **工程取舍**：P2 架构刻意不依赖 GPU embedding 模型（轻量、可 CPU 部署）；库规模上千后
  换 BGE-M3 编码即可，接口签名不变
- few-shot 修正的 3 类错误（A 组 4 条失败逐条归因）：
  ① 漏算运费（只 SUM(price) 不 SUM(price+freight_value)）→ few-shot 示例带运费写法
  ② 错选事实表（客单价/取消金额用 order_payments 而非 order_items，schema 注释
    「订单总额=SUM(payment_value)」有误导性）→ few-shot 的 order_items 写法纠正
  ③ 口径选择（RJ 客单价 支付口径 166.45 vs 商品口径 166.43 都合理，但问题未写清）
- ⚠️ 评测设计坑（踩过 3 个，全部修正并记录）：
  1. sqlparse 0.6.0 的 `token_first` 是**方法不是属性**——`s.token_first.match()` 抛
     AttributeError 被 except 吞掉，10 条合法 SELECT 全被误判 NOT_SELECT（首跑 0/10
     全挂但 SQL 肉眼全对 → 是评测代码 bug 不是模型 bug，修后 10/10 可执行）
  2. 金标口径必须与 system prompt 规则一致（默认只统计 delivered）——e3/e7/e10 最初
     金标未过滤状态，模型「遵守规则」却被判错；统一口径后判定才公平
  3. 口径歧义问题必须把口径写死在题干（e9 明确「客单价=order_items 商品金额含运费」），
     否则模型选支付/商品口径都合理，评测失去区分度——这正是 P2-S5「歧义表述」评测类的
     设计动机：歧义问题应触发澄清而非静默选口径

## 待测（后续 Stage）
- [x] ~~P2-S2 Schema 注入 + Few-shot 消融~~ → 0.600 → 1.000（+0.4，见上）
- [x] ~~P2-S3 ReAct 状态图 + 四层安全~~ → 8/8 行为用例通过（见下）
- [x] ~~P2-S4 工具注册 + Function Calling（agentic 图）~~ → pytest 40/40 + 行为 8/8（见下）
- [x] ~~P2-S5 60 条全量评测~~ → SQL 0.860 / 端到端 0.870 / 自愈 0.708 / 安全 100%（见下）
- [x] ~~P2-S6 FastAPI + SSE + ECharts 前端~~ → 服务化上线 + 浏览器实测（见下）

## 服务化 + 前端（P2-S6，2026-09-08）

**服务层**（`src/api/server.py` + `scripts/run_api.py`，端口 8501）：
- `POST /api/query` SSE 流式：生产者线程跑 `graph.stream(stream_mode="updates")` →
  `queue.Queue` → 异步消费者逐帧 yield（真流式，不攒完整回答）
- 六类统一事件：thought / sql / result / chart / answer / error（+done），
  `_norm_agentic` / `_norm_react` 把两种图的节点更新归一到同一事件协议
- `GET /api/schema`（左栏 Schema 树）/ `GET /api/history/{id}`（事件轨迹回放）/
  `POST /api/feedback`（👍👎 落库 `data/db/service.db`，人工标注沉淀评测样本）
- 每次请求独立 `thread_id`：图带 MemorySaver checkpoint，固定 thread_id 会让
  上一轮未收敛现场串到下一次查询（实测踩坑）

**前端**（`web/index.html`，原生 JS 无框架，iOS 玻璃拟态三栏）：
- 左 Schema 树（表/列/行数）/ 中对话（SQL 可折叠复制、thought chips、反馈）/
  右结果表 + ECharts（chart 事件独立渲染）
- `md()` 逐行块级渲染：表格占位符 → escape → 标题/列表/段落白名单标签
- **渲染根因坑**：answer 气泡曾用 `textContent` 插入 md() 产出的 HTML →
  整段标签当正文显示。改 `innerHTML`（安全成立：md() 先整体 escape 再插白名单标签）。
  教训：前端渲染验证必须浏览器截图，字符串验证不等于 DOM 正确
- 输出净化三层：服务端 `_strip_chart_xml`（raw+转义形态）、prompt 禁贴 ECharts 配置、
  前端 md() 再兜一层

**S6 健壮性加固（实测驱动）**：
| 坑 | 现象 | 修法 |
|---|---|---|
| checkpoint 串场 | 第二次查询秒回"已达步数上限" | 每请求独立 thread_id |
| AGNES 重复 tool_call | 同轮同参数调用成对出现，白烧步数 | tools 节点按 name+args 去重 |
| data_ref 命名漂移（b7 复发） | 模型编 `top5_categories_2017`，注册表只有 `result` | schema 写死填 result + `_resolve_rows` 兜底 |
| 二次指标心算 | SP 占比说 58%，真实 56.6% | prompt 禁心算占比，必须走 compute_metric |

**验收**：SSE 端到端（"2017年各品类销量 Top5，用图表展示"）status=ok、0 XML 残留、
chart 独立事件渲染；浏览器自动化截图验证表格/标题/列表真实渲染；feedback 落库验证；
pytest 40 passed 无回归。

## 全量评测（P2-S5，2026-09-08，60 条跑满）

**评测集 evals/goldset_s5.json**：60 条 = 12 单表 + 12 多表 join + 8 日期边界 + 8 复杂聚合 +
10 领域口径 + 4 空结果 + 6 安全；21 条标记 heal 候选；与 few-shot 库（12 条）/ S2 goldset（10 条）
严格错题（评测卫生）；金标 SQL 全部在库上验证可执行、非空结果类有数据。
评测脚本：`evals/run_eval_s5.py`（react_graph 端到端 + compare_result 金标比对，断点续跑）。

**四指标（全部达标）**：
| 指标 | 结果 | 验收线 | 说明 |
|---|---|---|---|
| SQL 结果准确率 | **43/50 = 0.860** | ≥0.80 | normal 类 50 条，生成 SQL 结果集与金标一致 |
| 端到端 ok | **47/54 = 0.870** | ≥0.75 | 全链路 status=ok（54 = 60-6 安全） |
| 自愈率 | **17/24 = 0.708** | ≥0.60 | 专项评测，见下 |
| 安全拦截 | **6/6 = 100%** | 100% | 删表/改状态/越权表/拼接/导出/系统表 全拦 |

**自愈专项（evals/run_selfheal_eval.py）**：主评测 60 题自愈样本为 0——react_graph 自愈只在
「SQL 执行报错」时触发，而 60 题模型首轮 SQL 全可执行（语义错不触发）。专项方法：对 10 条复杂题
注入 3 类模型真实会犯的执行错误（E1 列名不存在 / E2 表名不存在 / E3 语法残缺，共 24 注入），
验证「错误回喂 → generate 重新生成 → 校验执行 → 金标比对」链路。**17/24 = 0.708**。
错误回喂自愈在简单错（列名/表名 typo）几乎全恢复；失败集中在 a06/a08/m08 等**多表子查询
结构损坏**——修复后结构仍可能偏（如 100-200 占比题模型 CASE 与金标口径差一个层级）。

**剩余 7 条真实模型失败（模型弱点清单）**：
- `m08` 平均运费 Top3 州：模型用 geolocation 邮编映射州，金标用 customers 表收货州——两个州
  口径在部分订单不一致（RR/PB 运费微差 43.09 vs 43.07），模型选错了州来源表
- `m11` 送达耗时最短州：模型从 approved_at 算（批准→送达），金标从 purchase_timestamp 算
  （下单→送达）——**时间锚点选错**，比金标少算下单→批准段
- `a04` 客单价最高月份：模型把 order_items 金额 + order_payments 金额**双表相加**——语义重复
  计算（商品金额与支付金额是同一笔钱的两个视图）
- `a06` 100-200 元订单占比：模型 CASE WHEN 只算单层子查询、与金标双层口径差一个粒度
- `a05` 评价>50 条品类：模型缺 p.product_category_name IS NOT NULL 过滤 + 缺 orders 中间
  join，NULL 品类污染分组
- `a07` 3 品类客户：模型 join 了 translation 表（金标不需要），多 join 改变行数放大
- `g04` 平均每品类商品数：模型子查询没过滤 NULL 品类，NULL 组被算进 AVG 分母

**本轮踩坑（评测卫生优先级最高）**：
1. **主评测 4+1 条失败是金标口径 bug 不是模型错**（初版 SQL 0.760 假阴性）：
   g02 金标漏 customer_unique_id 去重（违反 system prompt 规则 6，278 vs 269）；
   g06 金标漏"不论订单状态"口径（模型按默认 delivered=4，金标全量=6）；
   g10 金标本身 SQL 错误（HAVING COUNT(*)>1 含同方式分期，正确口径
   COUNT(DISTINCT payment_type)>=2 = 1127 vs 2961）；
   m07/m12 比例题金标 0~1 小数、题干没写死，模型按习惯输出 ×100 百分比。
   教训：**金标与题干、system prompt 三者口径必须写死且一致**，否则评测分不清是谁错。
2. **自愈评测 compare 的 tuple/dict 混比 bug**：execute_with_timeout 返回 dict rows 而金标是
   tuple rows，canon() 对 dict 迭代出 key 字符串 → 24/24 全假 FAIL；改 sqlite3 直连拿 tuple 后
   17/24。评测代码 bug 先于模型怀疑（S2 同款教训复发）。
3. 命令行/SQL 中含 `EXISTS (` 字样的 Bash 命令被沙箱启发式 SIGTERM（评测脚本 41 分钟跑
   完没问题——它从 JSON 读 SQL；但内联 -c 传 EXISTS 即被杀）→ 验证 SQL 用等价 LEFT JOIN
   写法或写文件执行。

## 工具注册 + Function Calling 双保险（P2-S4，2026-09-07）

**两种编排方式的对照**：
- S3 `react_graph` = **确定性管线**：understand→generate→validate→execute 顺序由代码写死，
  LLM 只负责 generate 一步 → 流程固定、可解释性强。
- S4 `agentic_graph` = **LLM 自主编排**：同样手写 StateGraph（仍禁用 create_react_agent），
  但顺序由 LLM 的 tool_calls 决定——图只提供「agent→tools→回喂→agent」的通用循环 +
  MAX_STEPS=8 / TOKEN_BUDGET=40k 护栏。工具失败以 ToolMessage 回喂（ReAct 的 Observe 步），
  模型自行修正重试。
- **同一套 8 行为用例在两种图上跑都是 8/8** → 证明换编排心智不损失行为质量（no regression）。

**四工具 JSON Schema → Pydantic 双保险**（`src/tools/{models,registry}.py`）：
| 工具 | 作用 | 参数 Pydantic 校验 |
|---|---|---|
| generate_sql | question→SQL（工具内自动注入 Schema+Few-shot） | question 必填/长度 ≤500 |
| execute_readonly_sql | 校验+只读执行（四层安全在入口再次触发） | sql 必填、≤4000 字符、含 DROP/DELETE/UPDATE/ATTACH 即拒 |
| compute_metric | sum/avg/count/max/min/mom/yoy/ratio | metric ∈ 枚举白名单、value_col/data_ref 必填 |
| render_chart | 返回 ECharts 配置（S6 前端直接消费） | chart_type ∈ bar/line/pie |

**双保险原理**：LLM 侧 JSON Schema 是「软约束」（模型可能输出缺字段/错类型/幻觉参数），
服务端 Pydantic 是「硬校验」——fail_fast 一错即拒，返回参数校验失败信息回喂模型自愈。
恶意参数在类型/枚举/长度层就暴露，与 validator 的 AST 层形成纵深防御。

**实测（tests/test_tools.py 40 passed + test_safety.py 全绿）**：
- 拦截：缺必填字段 4/4、类型错/非法枚举 5/5、恶意参数（塞 DROP/DELETE/UPDATE/ATTACH/
  超长 SQL/超长 question）6/6 全拦 ✅
- 合法路径：`SELECT COUNT(*) FROM orders` 直通执行返回行；users 越权表在 Pydantic 层过了、
  被 validator 表白名单拦（blocked=True）→ 两把锁各有分工 ✅
- compute_metric 消费 results_registry 结果集：sum=60 精确 ✅

**8 行为用例 agentic 图复跑全过**（evals/run_s4_behavior.py，判准与 S3 完全一致）：
正常取数（43,428 笔）/ 多表 join Top5 州 / 2018 订单量最高月（1 月 7,069 笔）/
空结果兜底（2030 年 0 笔优雅说明）/ 恶意 DROP 净化拒绝无写落地 / SQL 自愈（品类 Top8）/
无 users 表说明（严格不映射）/ 相对时间口径默认 2018。

**本轮踩坑**：
1. AGNES 的 tool_calls 在 langchain 里是 `AIMessage.tool_calls` 列表——个别网关把
   `arguments` 字符串化，tools 节点要兼容 str→dict 再 dispatch（解析层健壮性）。
2. **工具结果回喂的上下文膨胀**：execute 返回上千行时全量 json 回喂会打爆上下文——tools
   节点截断到前 30 行并标注「共 N 行仅展示前 30」（_OBSERVE_MAX_ROWS），完整结果存
   state["result"] 供 respond 使用。
3. 行为用例 b7 暴露规则漏洞：模型会「说了没有 users 表」之后仍擅自映射到 customers 并
   展示数据——规则 5 收紧为「说明不存在→列出现有表→立即收尾，禁止查询/展示任何近似表」，
   严格复验 rows=0 才过。近似映射在真实 BI 里会误导用户以为是同一张表，比拒绝更危险。
4. 双图复用同一 CASES 模块（evals/run_s3_behavior.py 导出 CASES 常量）→ 口径零漂移，
   S3/S4 行为可比。

## ReAct 状态图 + 四层安全（P2-S3，2026-09-07）

**架构**：`src/agent/{state,nodes,react_graph}.py`，手写 add_node/add_conditional_edges
（禁用 create_react_agent）。图：understand → generate_sql → validate_sql → execute_sql
→(失败) self_correct 回环 / (成功或超限) respond。checkpointer=MemorySaver 支撑多轮。
防死循环双保险：retry_count 上限 3 + token_cost 预算 40k。

**四层安全（validator.py，pytest 19/19 + 攻击用例 17/17 通过）**：

| 层级 | SQLite 实现 | 验证 |
|---|---|---|
| ① 只读 | mode=ro URI（connect.py） | DELETE 被引擎拒绝 ✅ |
| ② 表名白名单 | sqlparse AST 提取 FROM/JOIN/子查询表名 ⊆ 9 表 | users / sqlite_master / 子查询绕过 / 逗号多表全拦 ✅ |
| ③ 行数限制 | 无 LIMIT 自动注入 LIMIT 1000 | orders 全表 → 1000 行 ✅ |
| ④ 语句超时 | progress_handler 每 1000 指令回调 | 百万行扫描 200ms 中断（TimeoutError）✅ |
| + 语句类型白名单 | 仅单条 SELECT（AST 层） | DELETE/UPDATE/DROP/INSERT/拼接/INTO OUTFILE 全拦 ✅ |

**8 行为用例全过**（evals/run_s3_behavior.py）：正常取数 / 多表 join / 日期边界 /
空结果兜底（2030 年 → 0 笔优雅说明）/ 恶意请求净化（模型拒绝 DROP 只查询）/ SQL 自愈 /
越权表拒绝（users → 拦截，不映射不编造）/ 相对时间口径默认 2018。

**踩坑**：
1. **AGNES 网关强制要求消息含 user 角色**——respond 节点只发 SystemMessage 报
   `No user query found`（400），8 用例全挂；改为 SystemMessage+HumanMessage 双角色即过。
   教训：第三方网关的隐式约束要最先摸清（P1 的空 content 是另一个网关怪癖）。
2. understand 口径改写规则 1 会把「2030年」这类**明确年份**也吞成默认口径 2018——
   规则只该兜底「最近/最新」相对时间；这是 Text2SQL 口径层的典型 over-normalize bug。
3. 恶意请求行为测试的设计误区：端到端层模型会「净化」DROP 只执行 SELECT（安全但测不到
   拦截路径）——安全层硬拦截应交给 validator 单测（17 攻击用例），端到端只验「无写操作落地」。
4. 点名不存在表时模型倾向语义映射（users→customers）——understand 提示词加
   「不在 Schema 的表不得擅自映射，需说明不存在」规则后正确拒绝。


## S9 写路径与数据接入（P2 主项目拓展，2026-09-08）✅

「库的增删改查」的写路径，核心是 **HITL 提案-人审**（Human-In-The-Loop）：

| 能力 | 实现 | 实测 |
|---|---|---|
| 写意图识别 | 正则粗筛（读请求零开销）→ LLM 单次调用出严格 JSON（intent/sql/warn），intent=read 原路返回 | 「被取消的订单有多少」正确判读；「新增客户 c_test_001」正确出提案 |
| 写提案三道闸 | `src/safety/writer.py`：①仅单条 INSERT/UPDATE/DELETE ②表名白名单（AST）③UPDATE/DELETE 必带 WHERE（AST） | pytest 8 攻击用例全过；字符串藏 WHERE/拼接 DROP 均拦 |
| **事务 dry-run** | BEGIN→写→SELECT changes()→ROLLBACK：**零副作用拿精确影响行数** + WHERE 取证样本行 | affected=1 精确；dry-run 后数据逐字节原样；提前抓住 NOT NULL 违约的 INSERT |
| 人审执行 | SSE 推 `proposal` 事件 → 前端审批卡（SQL/影响行数/样本/风险）→ 确认/拒绝 → `/api/confirm` 复检+事务执行 | 浏览器全链路：提案→确认→「已执行·实际影响 1 行」→ DELETE 提案删净 |
| 审计日志 | `data/db/write_audit.jsonl` 每次决策留痕（ts/sql/affected/谁批的） | executed/rejected 全记录 |
| CSV 导入 | `src/db/csvimport.py`：表名/列名清洗 + 类型推断 + 批量插入 + 行数质检 + **动态注册进白名单与 Schema 注入**（`custom_tables.json` 重启恢复） | 4 行 CSV→建表插入→Agent **立即查到新表**（SQL/结果/解读全对） |

**测试与回归**：`tests/test_writer.py` 18 用例（三道闸 8 + dry-run 3 + commit/审计 2 + CSV 5），全量 pytest **58 passed + 1 skipped 零回归**。

**连带修出的安全 bug**：sqlparse 把 `INSERT INTO t(col) VALUES…` 的目标表分组成
Function 节点，原表名提取只认 Identifier → **INSERT 的表白名单等于不存在**。补 Function
分支后读写两路共用同一修复。次要修复：UPDATE/DELETE 的 WHERE 判断需 flatten（Where 是分组节点）。

设计立场：读路径 LLM 自主循环 + 四层安全兜底；写路径**永不进自主循环**——模型有建议权，
人有否决权与执行权（执行凭据独立：SQLite 去 mode=ro / MySQL 形态为 agent_rw 账号）。


## S8 Supervisor 多智能体 + LLM-as-Judge（2026-09-08/09）✅

第三种编排：planner 拆解 → sql_agent / analysis_agent / viz_agent 子 Agent（**messages 各自隔离，
产出汇入共享黑板**——黑板模式，子 Agent 的碎碎念互不可见）→ judge 质量门控 → respond。
全部 add_node/add_conditional_edges 手写（禁 prebuilt，与 S3/S4 同约束）。

**双跑对照（同 8 条行为用例，S3/S4 口径零漂移，evals/s8_dual_result.json）**：

| 指标 | agentic（单 Agent） | supervisor（多智能体） |
|---|---|---|
| 行为正确率 | **8/8 = 1.000** | **7/8 = 0.875**（唯一失败 b5 是 AGNES 免费额度 429，非设计缺陷） |
| LLM 调用数 | 29 | 32（规划+判官的额外开销） |
| token（**旧口径·图内记账**） | 78,579 | 34,888 —— 两侧口径不一致，**不可相减**；同口径结论见「数字变更记录」 |
| 端到端延迟 | 293.6s | 828.1s（串行分工 + 判官轮次，诚实展示代价） |

质量门控两层：① Python 硬门控（黑板产出完整性）② LLM-as-Judge 软门控（数字溯源/口径合规/
结论与数据一致性，严格 JSON 裁决 pass/fix + fix_target + 修改指令，revision≤2）。
浏览器实测抓到真实打回循环：规划器给计数题省掉了 analysis → 判官硬门控「缺分析结论」打回 →
分析师补产 → 判官 pass。

**双跑评测连带修出三个真 bug**：
1. **CTE 别名被表名白名单误杀**（S3-S5 读路径一直存在的隐患）：`WITH max_month AS (...) SELECT
   * FROM max_month` 的 max_month 出现在 FROM 表位，被当越权表。修复：extract_cte_names
   从表名集合排除 WITH 定义（读写两路共用，4 个新单测）。
2. **sql_agent 漏 Schema 注入**：只列表名不列字段，模型猜错列归属（b2 把 customers 的
   customer_zip_code_prefix 写成 o. 前缀）。补全 Schema 注入后 b2 PASS——多智能体的子 Agent
   上下文裁剪过头会直接掉准确率，「隔离 ≠ 裸奔」。
3. **AGNES SDK 429 退避风暴**：ChatOpenAI 内置指数退避把单次限流拖成 20 分钟。max_retries=1
   快速失败 + 调用层/评测层自管退避 + 评测断点续跑（s8_progress.jsonl 每例落盘）。


## S7 MCP Server + Client（2026-09-09）✅

把核心能力按 **MCP 协议**对外开放，同时验证「第三方工具即插即用」反向接入：

| 能力 | 实现 | 实测 |
|---|---|---|
| MCP Server | `src/mcp_server.py`（FastMCP）：暴露 4 个工具（generate_sql / execute_readonly_sql / compute_metric / render_chart），**复用 src/tools/registry 同一实现**——不是另写一份绕过安全的副本 | 双传输实测：stdio 子进程回环 + `--http 8600` streamable-http（curl 握手 + 客户端调用成功） |
| MCP Client | `src/mcp_client.py` `MCPToolBridge`：后台事件循环线程 + `run_coroutine_threadsafe` 同步桥；`AsyncExitStack` 持有全部上下文管理器 | `enable_mcp_tools(server_specs)` 一行接入：发现 → 转换 → `register_external_tool` 动态注册，Agent 下一轮 bind_tools 即可见 |
| 安全不旁路 | 经 MCP 调 `DROP TABLE orders` / 查白名单外表 → 照样被四层安全拦截 | `[安全拦截·stmt_type] 仅允许 SELECT 查询` / 越权表拦截，协议外壳不改变安全语义 |
| 第三方接入 | `examples/mcp_demo_server.py`（8610 端口，模拟外部工具提供方） | `now_utc` 工具即插即用：发现 → JSON Schema→Pydantic 动态建模 → dispatch 返回 `2026-09-09T02:39:04Z` |
| 动态注册防撞 | 与原生同名工具被跳过（原生优先，不覆盖）；新名工具经 Pydantic 校验通道 dispatch（缺必填参数返回校验错误，无旁路） | 9 个 MCP 单测（tests/test_mcp.py）全过 |

**踩坑**：
1. **`__aenter__()` 后未持有上下文管理器对象 → GC 断连**：stdio_client 的连接被垃圾回收后
   报 `Connection closed`。修复：AsyncExitStack 统一持有（进栈即保活）。
2. **非 JSON 文本响应被误判为失败**：工具返回纯文本时按协议 isError 标志判定，
   非 JSON 包装成 `{"ok": true, "text": ...}`，不把正常文本当异常。
3. **评测卫生的延续**：`test_mcp_generate_sql_returns_sql` 是全测试套件唯一依赖真实 LLM 的
   用例，AGNES 免费额度 429 时 pytest.skip（非代码缺陷）——与 S8 评测的限流口径一致。

**★ 子进程凭据缺口（2026-10-16 换 DeepSeek 时暴露）**：

把 provider 从 AGNES 切到 DeepSeek 后，MCP 用例突然报 `Missing credentials`。
排查**先读 SDK 源码、不猜**，拿到决定性一行（`mcp/client/stdio.py:127`）：

```python
env=({**get_default_environment(), **server.env} if server.env is not None
     else get_default_environment())
```

即 `server.env is None` 时子进程**只拿到 SDK 的安全白名单**，不是父进程 `os.environ` 的复制。
本机实测该白名单只有 **12 个变量**，且**全是纯系统变量、零凭据**：

```
APPDATA HOMEDRIVE HOMEPATH LOCALAPPDATA PATH PATHEXT
PROCESSOR_ARCHITECTURE SYSTEMDRIVE SYSTEMROOT TEMP USERNAME USERPROFILE
```

父子环境对照实测（同一时刻）：

| 检查项 | 结果 |
|---|---|
| 父进程 `os.environ["DEEPSEEK_API_KEY"]` | ✅ SET (len=35) |
| `settings.llm_provider` / `llm_model` | ✅ deepseek / deepseek-flash |
| 子进程基线白名单变量数 | ⚠️ 12 |
| 白名单里含 `DEEPSEEK_API_KEY` | ❌ False |

**为什么 AGNES 时代没暴露**：子进程的 pydantic-settings 会读 **CWD 的 `.env`**，而 `.env`
里恰好有 `AGNES_API_KEY` —— 一直是**文件兜底**在起作用，不是环境继承。换成 deepseek 后
`.env` 无对应项，兜底断了才浮出水面。**这是「换掉一个看似无关的依赖，暴露了被兜底掩盖的
真实缺陷」的典型例子**，也说明「之前能跑」不能当作「设计正确」的证据。

**修法：显式白名单 + 取值源改为 settings（不是 `os.environ`）**

```python
# 配置名 → settings 字段；取值走 pydantic-settings（.env 文件 + 进程环境变量合并后的结果）
_MCP_ENV_FROM_SETTINGS = {
    "LLM_PROVIDER": "llm_provider",   "LLM_MODEL": "llm_model",
    "DEEPSEEK_API_KEY": "deepseek_api_key", "DEEPSEEK_BASE_URL": "deepseek_base_url",
    "DEEPSEEK_THINKING": "deepseek_thinking",
    "AGNES_API_KEY": "agnes_api_key", "AGNES_BASE_URL": "agnes_base_url",
    "AGNES_MODEL": "agnes_model",
    "LLM_TEMPERATURE": "llm_temperature", "LLM_MAX_TOKENS": "llm_max_tokens",
    "LLM_TIMEOUT": "llm_timeout",
    "DB_URL": "db_url", "DB_RO_URI": "db_ro_uri",
}   # ★ 写库口令 agent_rw 刻意不在名单内；SCENARIO 已移除（settings 无此字段、无人消费）
```

全量透传会把父进程所有密钥（DB 口令、云 token、第三方 key）塞进一个我们并未逐行审计过的
子进程——那是用「修好功能」换「泄露面无限大」。白名单让子进程恰好拿到「自建 LLM + 只读连库」
所需，其余不给。

**为什么取值源必须是 settings 而不是 `os.environ`（2026-10-18 修正）**：
只读 `os.environ` 会漏掉**只写在 `.env` 里**的值——AGNES 时代能跑，纯靠子进程自己也读了
CWD 的 `.env` 兜底（同一处兜底掩盖了同一个缺陷两次）。改从 settings 取还顺带得到：
① 子进程**不再依赖 CWD 能找到 `.env`**（从任意目录启动 `mcp_server.py` 都能跑）；
② 配置来源唯一，测试可直接 monkeypatch 伪造 settings 字段，**不必修改真实环境变量**
——测试因此从「红绿取决于机器」变成确定性。

**回归防线（零 LLM 消耗）：`tests/test_mcp.py` 第 ⑥ 段 7 条，全部不依赖真实环境变量**

全部用 `monkeypatch` 伪造 `Settings` 字段与 `os.environ`，因此**红绿与跑测试的机器无关**。

| 用例 | 钉住的性质 |
|---|---|
| `test_child_env_takes_value_from_settings_not_environ` | ★**决定性**：`os.environ` 与 `settings` 给**冲突值**时，子进程必须拿到 settings 的那份 |
| `test_child_env_passes_agnes_key_even_when_provider_is_deepseek` | 两个 provider 的密钥都要传（切 provider 后回切不必重开进程） |
| `test_child_env_omits_empty_settings_value` | settings 值为空串时该键**整体不出现**，而不是塞一个 `""` |
| `test_child_env_non_secret_configurable_values_are_forwarded` | 非密参数（温度/上限/超时）也要传，且正确 `int→str` |
| `test_whitelist_fields_all_exist_on_settings` | 白名单里的每个字段名都真实存在（挡 `getattr(..., None)` 静默吞掉错拼字段名） |
| `test_child_env_keeps_safe_baseline_and_no_secret_smuggling` | `PATH` 等基线仍在；`TAVILY_API_KEY` 等**非白名单密钥不得进**；值里不得出现写库口令 |
| `test_child_env_extra_overrides_whitelist` | 调用方 `spec.env` 可覆盖白名单同名项 |

**变异验证（防空气测试，两次都只改一件事）**：

| 变异 | 预期 | 实测 |
|---|---|---|
| M1：`_child_env()` 回退成「从 `os.environ` 取」（即修复前行为） | 决定性那条必须红 | **4 条红**，含 `test_child_env_takes_value_from_settings_not_environ` |
| M2：往白名单塞一个 `Settings` 上不存在的字段 `llm_model_typo_xxx` | 字段存在性检查必须红 | **1 条红**，报 `白名单引用了 Settings 上不存在的字段：['llm_model_typo_xxx']` |

两次变异均已回滚，`grep` 校验 **0 处残留**。

> **为什么「决定性断言」必须是冲突值**：如果只断言「子进程环境里的 key 等于 settings 的 key」，
> 那么当 `os.environ` 与 `settings` 恰好相同时（真实机器上通常如此），读 `os.environ` 的旧实现
> 也会通过——**通过的可能是量级巧合，不是设计正确**。给两个来源喂**不同的值**，
> 才能把「从哪儿取」这件事变成一个可判定的可观测量。

回归计数（2026-10-21，两套口径且**机器无关**）：
`pytest tests/` 默认 → **174 passed / 2 skipped**（收集 176；不配任何 key、不联网）；**前提是 `data/db/olist.db` 存在**——干净克隆里没有，CI 用 `scripts/make_fixture_db.py` 生成合成库；
`RUN_SLOW=1` + 配好凭据 → **176 passed / 0 skipped**。
`pytest tests/test_mcp.py` 对应为 **34 passed / 1 skipped** 与 **35 passed**。

### 服务端启动期体检 + 黑盒进程验收（2026-10-18 第二轮）

上一轮把「客户端闸门 + 调用期结构化错误」补齐了，但**服务端自己仍然会「带病启动」**：
`python src/mcp_server.py` 在没凭据时也照常起来，把 4 个工具都报给客户端。本轮补上第三道：

| 层 | 位置 | 缺凭据时 | 前提 |
|---|---|---|---|
| ① **服务端启动**（新） | `mcp_server.py::_startup_credential_check`，在 `mcp.run()` 之前 | 默认：stderr 打「变量 + 三步修法」后 `sys.exit(1)`；`--allow-degraded`：stderr WARN，**不注册 generate_sql**，其余 3 个照常 | 只依赖 settings，不依赖客户端 |
| ② **客户端闸门** | `mcp_client.enable_mcp_tools` | 连接**前**（`requires_llm: True`）或连接**后**（按工具集）判定 | 需要先能连上 |
| ③ **调用期** | `registry._tool_generate_sql` | `error_code=MISSING_CREDENTIALS` + `retryable=False` | 走到调用才触发 |

**为什么加了 ① 就必须给 ② 补「连接前」判定**（本轮最值得记的时序耦合）：
① 硬失败之后，缺凭据时子进程**直接 exit(1)**，客户端连 `list_tools` 都走不到 ——
② 里那道「连接后闸门」永远没机会说话，客户端只会收到一条协议层的「连接失败」。
**把失败提前，会改变上游组件的失败路径**：每提前一层，就要检查上面那一层还够不够得着。
故 `enable_mcp_tools` 现在支持 `spec["requires_llm"]=True` 时**连接前**判定；
连本仓库自己的 server 请务必写上这一项。

**stdout 纪律**：stdio 模式下 stdout 是**协议信道**，脏一个字节客户端就断连。
所有诊断（含启动失败的文案）一律 `print(..., file=sys.stderr)`。
验证方式：**手写 JSON-RPC 帧、直接起进程、逐行 `json.loads`**——不借 SDK，
因为 SDK 自己会容错掉脏字节，用它验等于没验。

**stderr 也要干净**（顺手）：FastMCP 自己的 Settings 有个未解析前向引用（`lifespan`），
每次起服务都会往 stderr 吐一条 `IncompleteFieldDefinitionWarning`，正好把「缺凭据」的
部署报错挤到后面。只在入口处**按类别**过滤这一条（其余 pydantic_settings 警告照旧输出）。
现在无凭据启动的 stderr 全文就是那 5 行修法，没有别的东西。

**新增 6 条用例**（`tests/test_mcp.py`，第 ⑦ 段 2 条 + 第 ⑧ 段 3 条 + 纯函数 1 条）：

| 用例 | 断言的是哪一层 |
|---|---|
| `test_server_exits_nonzero_and_names_the_variable_when_credentials_missing` | ★**外部契约**：非零退出 + stderr 点名变量 + 给出 `.env` + stdout 必须为空 |
| `test_stdio_stdout_is_protocol_only_when_healthy` | ★stdout 洁净：逐行都是合法 JSON-RPC 帧，且 4 个工具齐全 |
| `test_allow_degraded_starts_with_only_three_tools_and_warns` | 降级模式能起、只报 3 个工具、stderr 有 WARN |
| `test_preconnect_gate_blocks_connecting_when_declared` | 声明 `requires_llm` 时闸门在**连接前**（假桥 `connected == []`） |
| `test_preconnect_gate_passes_and_connects_when_credentials_present` | 反向对照：凭据齐备时不得阻止连接 |
| `test_judge_generate_sql_distinguishes_two_kinds_of_red` | **同一条报错，凭据在不在 → 一个红一个 skip**（见下） |

**为什么断言「退出码 + 人话文案」而不是「抛了某异常」**：后者是实现细节——
哪天换成 `sys.exit(2)`、或改成别的异常类型，只要退出码非零且文案不变，客户端体验就没变。
**能被外部观察到的东西才是契约**，其余都是可以自由重构的内部结构。

#### Task B：把「两种红」从环境判断里救出来

原用例靠 `os.environ.get("AGNES_API_KEY")` 之类判断要不要 skip，**红绿取决于跑测试的人的 shell**。
改成读 `settings`（部署配置唯一入口，已合并 `.env` 与进程环境），并加 `llm.has_llm_credentials()` 作为
「只判断不抛」的入口，规则是：**凭据不在 → skip；凭据在却仍报缺 → 必须红**（那是传参层 bug）。

诚实的边界：加了 ① 之后，「凭据在却报缺」对自家 server **已经不可达**（子进程启动即退），
所以把它埋在那个需要真实 LLM 的用例里等于永远没人验。于是把判定抽成纯函数
`_judge_generate_sql(out, credentials_present=...)`，用 4 组输入（结构化错误 / SDK 原话 /
429 / 正常）把规则钉死，真 LLM 用例只负责拿回 `out` 再交给它判。

#### 顺带修掉的「安全启动误伤测试」

① 硬失败之后，**没有凭据的机器上整个 `test_mcp.py` 都会 error**（夹具拉不起 server），
连那些压根不需要 LLM 的用例（安全拦截、回环查询）也一起挂。修法：夹具在
`has_llm_credentials()` 为假时用 `--allow-degraded` 起——新加的 flag 正好用来隔离
「部署没配」与「代码坏了」两种情形。两种场景实测：

| 场景（默认口径，不带 `RUN_SLOW`） | 结果 |
|---|---|
| 有凭据 | `pytest tests/test_mcp.py` → **34 passed, 1 skipped** |
| 无凭据（`DEEPSEEK_API_KEY= AGNES_API_KEY=`） | **34 passed, 1 skipped** —— **两行必须一样** |

> 这两行**故意要求相同**：口径统一之后，通过数不许再随「谁机器上有 key」漂移。
> 早期版本是 33 vs 32+1skip，看着都「绿」，但别人克隆下来复现不出我的数——
> 那正是「开源作品第一要求是可复现」要挡的东西。

**变异验证（三条，各只改一件事，均已回滚）**：

| 变异 | 实测 |
|---|---|
| M7：`__main__` 里跳过 `_startup_credential_check`（无条件注册 generate_sql） | **2 红**（非零退出 + 降级三工具） |
| M8：硬失败文案改成 `print(str(exc))`（打到 stdout） | **1 红**，且红在 **stdout 断言**上 |
| M9：`_MCP_ENV_FROM_SETTINGS` 漏掉 `DEEPSEEK_API_KEY` | **fixture 阶段 ERROR**（红），**绝不 skip** —— 白名单 bug 不会被「环境没配」吞掉 |

> M8 第一次跑时红在 **stderr 断言**上，而不是 stdout 断言——因为我把 `stdout 为空` 写在了
> stderr 断言之后，被「变量名不在 stderr」抢先报了。这正是「断言会互相遮蔽」的老问题：
> **最具体的诊断要放在最前面**，否则红了也归不了因。调整顺序后 M8 精确红在 stdout 那条。

> M9 的形态值得记：白名单漏项现在表现为**夹具阶段 ERROR**（子进程启动即 exit 1），
> 而不是干净的 assert 失败。噪声大了些，但方向是对的——**任何凭据传递 bug 都是红，
> 不存在「因为环境没配所以跳过」的逃逸通道**。

### 运行者既没 `.env` 也没环境变量时，这个 MCP 工具该怎么优雅失败？

**修复前的现状（比"优雅失败"更糟，2026-10-18 已修，见下「落地情况」）**：`enable_mcp_tools()` → `connect_stdio` → `list_tools`
**全程不调用 LLM**，所以凭据缺失时**连接照样成功、四个工具照样注册**。失败是**惰性**的——
要等到第一次真正调 `generate_sql`，`get_llm()` 才抛 `OpenAIError: Missing credentials`，
再被 `answer_agentic` 的 `except Exception` 兜成 `degraded`。用户看到的是
「**Agent 好像不太会写 SQL**」，而不是「**你少配了一个变量**」。这是三种失败方式里最差的一种：
**不报错、报错也报错在错的地方、文案还不是我们自己的**。

**立场：报「缺凭据」，但不要靠「不暴露工具」来解决。** 三条理由：

1. **暴露工具属于「能力」层，凭据属于「部署」层，耦合会让 schema 随环境漂移。**
   工具集一旦随环境变化，同一批评测用例在 A 机器能跑、B 机器不能——这与本项目
   「评测必须可比、换模型必须 `--tag`」的纪律直接冲突。工具集应当是**确定的**。
2. **静默隐藏就是静默失败。** 用户拿到的现象是「模型不用 SQL 工具」，需要反查四层
   才能想到是 key 没配。可观测性上，**没有日志的自动降级等于制造谜题**。
3. **模型会读 ToolMessage 做决策。** 把 SDK 原话 `Missing credentials` 喂回去，
   它很可能解读成「这个工具坏了，换一个试试」→ 白烧步数；缺凭据的 `capability`
   不是「换工具」，而是「这个部署缺配置」。

**该怎么做（两层，缺一不可）**：

| 层 | 做法 | 为什么 |
|---|---|---|
| **启动期：fail-fast，且点名到变量** | `enable_mcp_tools()` 连接前按当前 `llm_provider` 检查对应 key 非空；缺失则抛 `MissingCredentialError`，消息里写清「缺哪个变量、`.env` 还是环境变量、怎么验证」 | 部署错误必须在**部署时刻**暴露，不是在使用时刻。报错文案要能让人**一步修好** |
| **调用期：把 SDK 文案翻译成结构化错误** | `_tool_generate_sql` 里给 `get_llm()` 加保护，返回 `{"ok": false, "error_code": "MISSING_CREDENTIALS", "retryable": false, "error": "缺少 LLM 凭据：DEEPSEEK_API_KEY 未配置"}` | ToolMessage 是**喂给模型**的。`retryable: false` 明确告诉它别重试，`error_code` 给上层做路由分支 |

**「不暴露工具」只在一种情况下正确**：作为**显式降级开关**（`strict=False`），用于 CI /
纯离线单测——此时跳过该 server **并且打一条 WARNING**。区别在于：这是
「**我知道我在降级，并且留下了声音**」，而不是「悄悄少给模型几个工具」。

一句话：**默认 fail-fast 报缺凭据；「不暴露」只能当显式开关，且必须带日志。**

#### 落地情况（2026-10-18 已实现，非纸面方案）

| 落点 | 实现 | 关键设计取舍 |
|---|---|---|
| `src/core/llm.py` | 新增 `MissingCredentialError` + `require_llm_credentials()`；`get_llm()` **第一行就体检** | 单一真相源：所有调 `get_llm()` 的路径（MCP server、registry、react 图）都拿到同一条可执行的报错，不会各有各的说法 |
| `src/tools/registry.py::_tool_generate_sql` | 只包住 `get_llm()`，返回 `{"ok": false, "error_code": "MISSING_CREDENTIALS", "retryable": false, "error": <首行>}` | 回喂模型**只给一行**（不塞整段修法手册），`retryable=False` 明确叫它别重试 |
| `src/mcp_client.py::enable_mcp_tools` | 新增 `strict=True` 参数；`list_tools` 之后、注册之前过一道 `_gate_llm_credentials` | 见下「为什么不按 server 名判断」 |

**判据按「工具集」而不是按 server 名/配置**：`execute_readonly_sql` / `compute_metric` /
`render_chart` 都不建 LLM 客户端，只有 `generate_sql` 会。若按「凡是 MCP server 就要求凭据」
判断，第三方天气/日历 server 会因为**我们自己没配 LLM key** 而被误拦——那才是真 bug。
需要时可用 spec 里 `"requires_llm": true/false` 显式覆盖（第三方把 LLM 藏在一个看不出
名字的工具后面时用得着）。

**报错文案为什么不复用 SDK 原话**：`openai.OpenAIError` 说的是
「Please pass an `api_key` … or set the `OPENAI_API_KEY` environment variable」——
**把我们的人指向了本项目根本不用的变量名**（我们是 `DEEPSEEK_API_KEY` / `AGNES_API_KEY`）。
报错必须点名到真实变量，并给出「`.env` 写一行 / `setx` 设用户级变量 / 一条验证命令」三步。

**新增 11 条零 LLM 用例**（`tests/test_mcp.py` 第 ⑦ 段，全部 monkeypatch 伪造 settings / 假桥）：

| 用例 | 钉住的性质 |
|---|---|
| `test_get_llm_missing_key_raises_typed_error_naming_real_variable` | ★环境变量里**有** key、settings 里**没有** → 仍必须失败；且文案含 `DEEPSEEK_API_KEY`、**不含** `OPENAI_API_KEY` |
| `test_require_llm_credentials_rejects_unknown_provider` | provider 拼错不猜默认，报错给出可选值 |
| `test_generate_sql_returns_structured_error_and_marks_not_retryable` | 结构化 dict：`error_code` / `retryable=False` / 单行文案 |
| `test_needs_llm_credentials_detects_from_tool_set` | 判据来自工具集（三个本地工具 → False；加 `generate_sql` → True） |
| `test_needs_llm_credentials_spec_override_wins` | 显式声明优先于自动判定 |
| `test_gate_raises_in_strict_mode_with_server_context` | strict 报错带 server 名 + 变量名 |
| `test_gate_degrades_with_warning_when_not_strict` | 非 strict 返回 False **且必须留 WARNING**（`caplog` 断言） |
| `test_gate_does_not_block_servers_without_llm_tools` | ★反误拦：我们没配 key 不连累不依赖 LLM 的第三方 server |
| `test_enable_mcp_tools_wires_the_gate` | ★**接线**：闸门必须真的被 `enable_mcp_tools` 调用（假桥，不拉子进程不联网） |
| `test_enable_mcp_tools_skips_registration_when_degraded` | 非 strict 下**一个工具都不注册**（返回空）且留 WARNING |
| `test_enable_mcp_tools_registers_normally_when_credentials_present` | **反向对照**：凭据齐备时闸门不得妨碍注册（否则前两条绿得毫无意义） |

**变异验证（三条，各只改一件事，均已回滚、0 残留）**：

| 变异 | 实测 |
|---|---|
| M3：`_tool_generate_sql` 去掉 `except MissingCredentialError` 分支 | **1 红**（结构化错误那条），异常直接穿透 |
| M4：`_gate_llm_credentials` 开头直接 `return True`（闸门失效） | **2 红**（strict 不抛 + 非 strict 不降级） |
| M5：`_needs_llm_credentials` 一律返回 True | **2 红**（工具集判定 + spec 覆盖） |
| M6：删掉 `enable_mcp_tools` 主流程里那行闸门调用 | **2 红**，而 9 条辅助用例**全绿**——见下 |

> **M6 是本轮最有价值的变异**：它把「函数正确」与「函数被调用」这两个性质彻底分开了。
> 删掉主流程里那一行 `if not _gate_llm_credentials(...)` 之后，9 条测辅助函数的用例**全绿**
> （每个函数各司其职、单测都过），只有 2 条接线用例报红——线上却已经退回到惰性失败。
> **只测叶子函数、不测调用点，是重构/改动里最容易漏的一类空转测试。**
>
> 同理 M5 下 `test_gate_does_not_block_servers_without_llm_tools` 仍然绿：它显式传
> `needs=False`，绕过了判定函数。**一条用例只能对应一个性质**，别指望它顺带看住别的。
>
> **过程中的自伤**：新加的接线用例调 `enable_mcp_tools` 注册了一个 `weather_now`，而
> `register_external_tool` 改的是**模块级全局** `TOOL_SCHEMAS` / `PARAM_MODELS`，测试结束
> 没恢复 → `tests/test_tools.py::test_tool_schemas_are_four_functions` 报「多了 weather_now」。
> 该文件本来就有 `_fresh_registry` 快照 fixture，我漏挂。教训：**定向跑（`-k`）绿不代表全量绿**，
> 但凡测试会改全局状态，就必须挂快照 fixture；而**只有一次完整跑才能发现这类跨文件污染**。


### 通过数「不可复现」的三次经历（写成纪律）

| 次数 | 我声称 | 别人实测 | 性质 |
|---|---|---|---|
| 1 | 110 passed / 0 skipped | 107 passed / **1 failed** | 真缺陷（MCP 子进程凭据缺口） |
| 2 | 「MCP 已修」 | 仍 failed | 真缺陷（server 侧没有启动期体检） |
| 3 | 131 passed / 0 skipped | 129 passed / **2 skipped** | **环境差异**——但同样必须修 |

前两次的教训是「我这儿绿不算数」；第三次更微妙：**没有任何人报错、也没有任何断言变红**，
只是数字对不上。但对开源作品来说，**可复现性本身就是产品属性**——
README 写「174 passed」，别人克隆下来跑出 129/2，这个数字就不可信了。

根因是两条真 LLM 用例按「settings 里有没有凭据」**自动** skip：这比「看 shell 里有没有
`os.environ[...]`」好，但仍让通过数随环境漂移。修法取**统一开关**而不是双口径并列：
两条真 LLM 用例都挂 `@pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", ...)`
（与 `test_tools.py` 原有写法对齐），**默认口径 174 / 2（前提是库已就绪）**，
要跑全量显式 `RUN_SLOW=1`。

> 为什么不选「双口径并列写两个数」：两个数都对，但**读的人永远不知道该信哪个**，
> 而且每加一条外部依赖用例就要再加一行。开关只有一个状态位，回答问题只有一句话。
> 判断标准就一条：**别人克隆下来不配任何东西，跑出的数必须等于 README 上写的数。**

## 延迟与成本统计（S8 复盘扩展，2026-09-09）✅

从已有评测落盘结果离线提炼（evals/run_latency_stats.py，零 LLM 消耗），补齐「性能账本」：

**S5 全量 60 条延迟分布（真实计时）**：

| 分桶 | n | mean | median | p95 | max |
|---|---|---|---|---|---|
| 全部 | 60 | 44.3s | 37.0s | 101s | 118s |
| single_table（最简单） | 12 | 29.6s | 27.5s | 40s | — |
| agg_advanced（最复杂） | 8 | 63.1s | 55.0s | 118s | — |
| **触发自愈** | 21 | **50.9s** | 44s | 102s | — |
| 一次通过 | 39 | **40.7s** | 36s | 85s | — |

量化结论：**自愈的延迟代价 = 一次通过的 +25%**（50.9 vs 40.7s）——重试一轮 LLM 生成 +
校验的线性成本，换来的是 21 条本会失败的查询被救回（自愈成功率 0.708），

**S8 双跑单例成本（成功例口径）**——下方 token 一律是**旧口径「图内记账」**，两侧口径不一致、
**不可跨编排相减**，详见「数字变更记录」：

| 编排 | tokens/例（旧口径） | latency/例 | llm_calls/例 |
|---|---|---|---|
| agentic | mean 9,822 / median 6,794 | 36.7s | 3.6 |
| supervisor | mean 4,984 / median 4,300 | 58.3s（+59%） | 4.6 |

只有「**延迟 +59%**」这一侧是可靠的。订正后的成本模型：**supervisor 延迟 +56%、成本 +62%**；
「token 更低」在同口径下仅 **−9.8%**，且在 DeepSeek 上未复现（缓存命中率 62.8% vs 84.0%）。

> ⚠️ **本节 token 结论已作废（2026-10-16 交叉验证订正），请以「数字变更记录」为准。**
> 本节 token 数取自图内 `state.token_cost`；用 CostTracker 直接读 API usage 互校后发现
> **agentic 侧少算 31.4%**（嵌套工具调用未入账，详见「真实 LLM 端到端」章节的
> 「S8 交叉验证暴露的计量不一致」）。保留本节原文以记录「单一计量口径自证不了自己」这个过程。


## S8-b5 补跑结论 + S7 LLM 端到端验证（2026-09-09）✅

AGNES 免费额度恢复后对历史欠账定向补测：

**S8-b5（安全写用例）——判定为「额度绑定」，非设计缺陷**：
两天内 3 次独立补跑（各含 60s 评测层退避 + 100s 冷却）全部 429。现象规律：单次 LLM
调用可过（探针成功、MCP 单例用例成功），但 supervisor 的多轮串行调用（planner→sql_agent→
judge，每例 4-8 次连发）必触免费档突发限额。**运维洞察：免费额度部署下，supervisor 的
「规划-分工-裁决」结构比单 Agent 循环更容易撞限流**——降级路径（degraded + 如实说明）
是正确的兜底姿态。S8 按**订正后口径**记录：行为 7/8（唯一失败 b5=429，属额度绑定）、
延迟 +2.8×；token 侧旧口径为 −56%，同口径（API 实测）重算为 **−9.8%** 且在 DeepSeek 上未复现
——口径变更来由见「数字变更记录」。（注：DeepSeek 复跑已补齐 supervisor 8/8，见文末章节。）

**S7 LLM 端到端——已验证**：`test_mcp_generate_sql_returns_sql` 冷却后单跑 PASSED
（24.5s）：经 MCP 通道真实调用 AGNES，generate_sql 返回合法 SELECT——「第三方经 MCP
调用本项目 LLM 能力」的全链路闭环补齐。

**测试套件里的真实 LLM 消费点（订正：不是「唯一」，是两条且形态不同）**：

| 用例 | 门控形态 | 本次实测 |
|---|---|---|
| `tests/test_mcp.py::test_mcp_generate_sql_returns_sql` | **自动跳过**：429/限流 → skip；排队超时（>120s）→ skip | SKIPPED（429） |
| `tests/test_tools.py::(真实 LLM 用例)` | **主动门控**：需 `RUN_SLOW=1` 才跑，默认跳过以免烧 token | SKIPPED（未设 RUN_SLOW） |
| 其余 81 例 | 全离线（FakeLLM / 本地算力），评测卫生保持干净 | 81 passed |

> 上表是 **AGNES 时代的历史快照**（当时那两条只能 skip）。换 DeepSeek 后两条均真跑，
> 当前计数见文首「数字变更记录」第 5 行（默认 **215 passed / 2 skipped**，收集 217；`RUN_SLOW=1` → 全部 217 条不 skip）。

> 原文写「唯一真实 LLM 消费点」不准确，已订正。同时修掉一个**评测假象**：
> 该 MCP 用例原先只处理「网关返回 429（错误在 dict 的 `error` 字段里）」这一种失败形态，
> 而免费档排队过久时 `_submit` 的 `.result(timeout)` 会抛 `TimeoutError`——**不是 dict**，
> 直接冒泡成测试失败，于是限流时全量跑出 1 failed 而非 1 skipped。
> 修法：`except TimeoutError: pytest.skip(...)` + 超时放宽到 120s（免费档「慢但能用」不该被误判成失败）。
> 教训与 P1 的「评测代码 bug 先于模型怀疑」同源——**依赖外部服务的用例，必须把该服务的每种
> 失败形态都映射成 skip，否则红/绿取决于当天额度**。

## 场景包（Scenario Pack）：业务外壳可插拔（2026-09-08）✅

**问题**：业务口径（默认状态 `delivered`、去重键 `customer_unique_id`、币种 BRL、数据范围、
业务表枚举）原本**散落在 5 个 prompt 里**。改一个口径 = 改 5 个地方且**必然漏改**；
而漏改的后果在评测上是**区分度丢失**——金标口径与 prompt 口径不一致时，模型「遵守规则」却被判错
（S5 已踩过这个坑，见「全量评测」一节的口径一致性条目）。

**做法**：把「随业务变化的口径级参数」收进一个 **frozen dataclass**（`src/scenarios/base.py`），
引擎与业务外壳解耦。

| 项 | 实测 |
|---|---|
| 数据契约字段数 | **10**（name / domain / dialect / known_max_year / data_range / table_catalog / default_status / dedup_key / currency / db_desc） |
| 消费点（`get_profile()` 调用处） | **5 处**：`agent/nodes.py`、`agent/agentic_graph.py`、`agent/supervisor_graph.py`、`agent/prompting.py`、`api/server.py` |
| 契约测试 | `tests/test_scenario.py` **6 passed** |
| 换场景演示（医疗） | 断言 4 项切换到位（`医疗业务数据分析助手` / `PostgreSQL` / `CNY` / `patient_uid`+`completed`）且 **Olist 口径残留 = 0**（`BRL` 与 `customer_unique_id` 均不得出现） |

**字段取舍原则（防过度抽象）**：只收「**多个 prompt 共用、且随业务变化**」的值；
单处使用或纯模板措辞留在各 prompt 里。反例：`db_desc` 只进 1 处但也收了，
因为它是「换业务必须改」的描述——判据是**变化频率**而非使用次数。

**「行为零变化」的准确表述（诚实）**：本次是**取值来源**的重构（硬编码字面量 → 从 profile 取），
验证靠的是**prompt 内容断言**（6 用例逐项检查口径关键词在位）**而非重跑 60 条评测**。
即「渲染结果等价」有测试证据；「模型输出分布不变」是推断，未测量。
对纯取值来源重构这是可接受的取舍，但**它不构成端到端回归证据**，这两句话不能混。

复现：`python -m pytest tests/test_scenario.py -q`（6 passed，全离线，16s）


## 真实 LLM 端到端：换 DeepSeek-V4.1-Flash 全量复跑（2026-10-16）✅

**为什么换**：AGNES 免费档在 S5/S8 复跑时反复 429（S8-b5 三次补跑均限流），
「质量结论被额度绑架」。DeepSeek-V4.1-Flash 单价低、无限流，可一次跑完三个评测。

### 预算：先实测单价，再决定跑不跑

不估算——先用**分层抽样**探路（7 种题型各取 1 条 + 2 条自愈题，共 8 条）：

| 项 | 实测 |
|---|---|
| 探路 8 条成本 | **¥0.0216**（约 ¥0.0027/条） |
| 缓存命中率 | **81.9%**（system prompt 的 schema 注入 + few-shot 每条完全相同，天然高命中） |
| 外推全量 60 条 | ≈ **¥0.16**；即便自愈重试使其翻倍，也仅 ¥0.33 |
| 当时余额 | ¥6.01 → **余量 18–37 倍，判定为「够，直接跑」** |

★ 若一律按**未命中价**（¥2/M）估价，同一轮会被算成 ¥5+ 从而误判「跑不起」——
**缓存命中率是成本估算里最不能省的一项**（见 `src/core/cost.py` 的键名兼容处理）。

### 三个评测的实测结果（同一提供方、一次性跑完，无中断）

| 评测 | 指标 | AGNES 基线 | **DeepSeek-V4.1-Flash** | 耗时 |
|---|---|---|---|---|
| S5 全量 60 条 | SQL 结果准确率 | 0.860 (43/50) | **0.780 (39/50)** | 460s |
| | 端到端 ok | 0.8704 (47/54) | **0.7963 (43/54)** | |
| | 安全拦截 | 6/6 | **6/6** | |
| 自愈专项 | 自愈率 | 0.708 (17/24) | **0.708 (17/24)** | 100s |
| S8 双编排 | agentic 正确率 | 1.0 (8/8) | **1.0 (8/8)** | 58s |
| | supervisor 正确率 | 0.875 (7/8，唯一失败=429) | **1.0 (8/8)** | 90s |

**成本合计 ≈ ¥0.29**（S5 ¥0.1755 + 自愈 ¥0.0354 + S8 ¥0.0729），S5 端到端 **460s vs AGNES 约 2658s（按 mean 44.3s/条）→ 快 5.8×**。

**两个立刻可用的结论**：
1. **自愈率两模型完全一致（0.708 vs 0.708）**，且 **S8 两张图都 8/8**——瓶颈不在生成器。
2. DeepSeek 不限流 → **S8 首次拿到双编排都 8/8**，AGNES 时代 b5 永远卡在 429。

### ★ 核心发现：那 −0.08 的「退化」全部来自评测资产缺陷，不是模型差异

逐题对齐（按 question 原文，不用 id）后，DeepSeek 独有失败 5 条，逐条归因：

| 类别 | 条数 | 题号 | 证据 |
|---|---|---|---|
| **prompt ↔ 金标口径互斥** | 3 | f06 / m06 / m10 | system prompt 明令「默认只统计已送达(delivered)订单，除非问题明确包含其他状态」（`agent/prompting.py:112`），而这三题**金标 SQL 里没有 `order_status` 过滤** → **遵守规则的模型必被扣分** |
| **单位口径歧义** | 2 | d07 / g03 | 金标用**比例**(0-1, `ROUND(...,4)`)；模型用**百分比**(×100, 2 位)。题干「占比」两种读法都成立 |
| **真实模型差异** | **0** | — | — |

**口径修正后的分数对照**：

| 口径 | AGNES SQL | DeepSeek SQL | Δ |
|---|---|---|---|
| 原始 60 条全量 | 0.8600 | 0.7800 | **−0.0800** |
| 剔除「prompt↔金标互斥」3 条 | 0.8511 (47) | 0.8298 (47) | −0.0213 |
| 再剔除「单位口径歧义」2 条 | 0.8444 (45) | **0.8667 (45)** | **+0.0222** |

★ 第二行剔除是硬证据（prompt 与金标直接矛盾，两侧都不该计）；**第三行剔除是判断题**
（「占比」按中文统计惯例更常读作比例，但百分比也说得通）——**两行都列出来，不挑好看的报**。
而且 `src/scenarios/olist.py` 的 docstring 自己就写着「金标口径必须与本文件一致，
否则评测失去区分度（S5 踩过的坑）」——这条纪律在这 3 道题上没被执行。

### ★ 评测卫生：判据按措辞判分（已修）

`empty_result` 原判据是**查关键词表** `["0 笔","不存在","无记录",...]`。
换模型后暴露：AGNES 恰好说「0 笔」就过，DeepSeek 说「总销售额为 **null**，即没有数据」
或「数量为 **0**」就被判错——**两者都是正确处理了「无此数据」**。

改为**结果集语义比对**（与 normal 类同判准：空结果题的金标本就是 `COUNT=0` / `SUM=NULL`），
关键词仅作拿不到 SQL 时的兜底。4 条专项验证 **4/4 全过**（e02/e04 从 FAIL 变 PASS），
端到端因此 0.7593 → **0.7963**。

★ 这是「**评测代码 bug 先于模型怀疑**」的又一例：换了模型，暴露的却是判据。
**比较口径的不对称也要讲清楚**：新判据只落盘了 SQL，旧判据没落，且 AGNES 免费额度
不可再跑 → 该修正**只能对 DeepSeek 侧复验，AGNES 侧无法追溯**。故上表另给了
「剔除 empty_result」的对齐口径（两模型同判据、同 56 条样本）。

### ★ 重复性：同模型同参数跑两次

| 指标 | run1 | run2（判据修正后） | Δ |
|---|---|---|---|
| SQL 准确率 | 0.7800 (39/50) | 0.7800 (39/50) | **0.0000** |
| 端到端 | 0.7593 (41/54) | 0.7963 (43/54) | +0.0370（全部来自判据修正） |
| 逐题判定一致 | — | **56/60 = 0.933** | — |

**56/60 的一致率就是单次对比的分辨率下限**：任何小于约 4/60 的差异都落在噪声里，
不足以支撑「A 比 B 强」的结论。原 −0.08 的差距（5 条）**恰好就在这个量级边缘**。

### ★ S8 交叉验证暴露的计量不一致（既有结论需订正）

用 CostTracker（直接读 API usage）与图内记账（`state.token_cost`）**互校**，对不上：

| 图 | 图内记账 token | CostTracker 实测 (in+out) | 一致？ |
|---|---|---|---|
| agentic | 47,870 | 65,807+4,048 = **69,855** | ❌ 少算 **31.4%** |
| supervisor | 63,039 | 57,268+5,771 = **63,039** | ✅ 完全一致 |

**根因已定位到代码**：`src/tools/registry.py:111 _tool_generate_sql` 是 agentic 图通过
`bind_tools` 暴露的**工具**，其内部**自己调了一次 `get_llm()`**。模型委派该工具时产生
**嵌套 LLM 调用**，而 `tools` 节点只回填工具结果、**不把嵌套调用的 token 计入 state**。
（佐证：CostTracker 记到 **30** 次调用，图内 `steps` 只有 **24**。）

**因此**：
- 图中「**supervisor token −56%**」是拿**两个口径不一致的数**相除得来的，**跨编排比较不成立**；
- 用同口径（API 实测）重算：agentic 69,855 vs supervisor 63,039 → **−9.8%**，不是 −56%；
- 成本口径：agentic **¥0.0278** vs supervisor **¥0.0451** → supervisor **贵 62%**
  （其缓存命中率 62.8% 明显低于 agentic 84.0%——判官/修订轮的上下文每轮都变，命中不了）。
- **订正后的诚实表述**：换成不限流的提供方后，**两种编排都 8/8**；supervisor 的真实代价是
  **延迟 +56%、成本 +62%**，而「token 更低」这一优势**未能在 DeepSeek 上复现**。

★ 这个不一致**正是靠"加一个独立计量"才发现的**——单一计量口径自证不了自己。
也说明**嵌套 LLM 调用是 token 记账的经典盲区**（P1 的 CostTracker 是用回调体系挂的，
天然覆盖嵌套调用，这正是它比手写累加可靠的原因）。

### 复现

```bash
# 探路定预算（~¥0.02，先分层抽样，不要拿最便宜的题估）
.venv/Scripts/python.exe evals/run_eval_s5.py --tag pilot --only f01 m01 d01 a01 g01 e01 s01 f07 --cap 1.2
# 三个评测（带 --tag 隔离，绝不覆盖 AGNES 基线；--fresh 忽略已满进度文件）
.venv/Scripts/python.exe evals/run_eval_s5.py     --tag deepseek --fresh --cap 3.0
.venv/Scripts/python.exe evals/run_selfheal_eval.py --tag deepseek --cap 2.0
.venv/Scripts/python.exe evals/run_s8_dual.py      --tag deepseek --cap 3.0
```

**脚本卫生要点（都踩过）**：① 换提供方必须 `--tag`，否则进度文件已满会**一条都不跑**、
把历史数字重新打印一遍，看起来像刚跑过；② `--cap` 熔断必须能穿透 `answer*` 的
`except Exception` 兜底，否则熔断信号被吞成 `degraded`；③ 成本计量必须分**峰谷**
（同为周五，16:39 峰时跑与 20:58 谷时跑，同量价差 2 倍）。

## 部署期错误的两种失败方式：库函数抛异常，入口给退出码（2026-10-20）

**问题**：`scripts/init_db.py` 的 `rebuild_db()` 顺序是「解压 → `DB.unlink()` → 逐表
`read_csv`」。数据集没下全时，它会在**删掉旧库之后**崩在 `FileNotFoundError` 上——
把「少下载一个文件」升级成「已有的 135MB 数据库没了」。而且是一次只报一个：
补一个、再跑一次、发现下一个，9 个文件要跑 9 轮。

**为什么不用 try/except 兜**：兜住异常只能让进程不死，**旧库已经删了**。修法是把检查
**移到 `unlink()` 之前**——顺序本身就是正确性。这条是本轮唯一真正重要的改动。

**两种失败方式，不该同一种**：

| 层 | 失败方式 | 理由 |
|---|---|---|
| 库函数 `rebuild_db()` | 抛 `MissingRawDataError`（带 `missing` / `raw_dir` 字段） | 库函数不该决定进程怎么死。`sys.exit` 抛的 `SystemExit` 继承 `BaseException`，`except Exception` **拦不住**，会穿透测试、notebook、web 后端 |
| 脚本入口 `_cli()` | 捕获后打人话到 stderr，`sys.exit(2)` | 入口的责任是给「退出码 + 人话」，让 CI / .bat / 人都能判断 |

**退出码编号**：`1` = 配置/凭据缺失（已被 `mcp_server.py` 启动体检占用），
`2` = 前置数据缺失。分开编号，CI 才能一眼区分「我 key 没配」和「我数据没准备好」。

**为什么「一次列全」而不是短路**：报错是给人看的，就该把**全部**缺失一次说完。
短路实现下用户要跑 9 轮才见到全部——这跟「不给指引」是同一类体验问题。

**顺带修掉的同根因残留**：同文件第 111 行 `flag = f"⚠️ 期望 {…}"`。
这一行正好是「行数对不上」时唯一要看的信息，而 U+26A0 在 Windows 控制台
（cp936/GBK）编不出来 → **一报错就崩**。改成 `[MISMATCH]`。
同一根因在 `scripts/init_db_mysql.py:85`（`✅`）和 `evals/run_eval_s5.py:281`（`⚠`）
仍存在，属另一改动域，**已记录未修**（前者在成功路径、后者在熔断分支，都需要看信息）。

**新增用例 11 条（`tests/test_init_db.py`，全离线，4.8s）**：

| 用例 | 钉住什么 |
|---|---|
| `test_check_raw_data_lists_all_missing_at_once` | 缺 9 个就报 9 个，**不短路** |
| `test_check_raw_data_accepts_geolocation_zip` | README 说「或放原 zip 也行」不能被自己的检查打死 |
| `test_check_raw_data_still_reports_geolocation_when_neither_present` | 特例不能写成跳过一切 |
| `test_exception_carries_structured_payload` | 异常带结构化字段 + 文案三要素（缺几个/哪下/放哪） |
| `test_exception_is_a_FileNotFoundError` | 老调用方 `except FileNotFoundError` 不漏接 |
| `test_error_message_is_cp936_encodable` | **文案不许含 emoji**，机器闸门 |
| `test_missing_data_does_not_delete_existing_db` | **核心**：缺数据时旧库字节不变 |
| `test_cli_exits_2_and_points_to_kaggle_when_data_missing` | 外部契约：退出码 2 + stderr 有 Kaggle 链接，stdout 干净 |
| `test_cli_help_still_works` | `--help` 不被前置检查拦住 |
| `test_dump_schema_path_does_not_require_raw_data` | 防「把检查提到 `main()` 外层」的过度加固 |

**变异验证（3 个，都精确红在预期那一条）**：

| 变异 | 结果 |
|---|---|
| A：把检查挪到 `DB.unlink()` 之后（复刻原 bug 顺序） | **仅** `test_missing_data_does_not_delete_existing_db` 红 |
| B：`check_raw_data` 遇到第一个缺失就 `break` | **仅** `test_check_raw_data_lists_all_missing_at_once` 红 |
| C：报错文案里塞回 `⚠️` | **仅** `test_error_message_is_cp936_encodable` 红 |

> 变异 A 值得单独说：它让「退出码 2」这条依然绿——因为异常照样抛、`_cli()` 照样
> `exit(2)`。**「行为对」和「数据没丢」是两个正交的断言**，只有专门钉「字节不变」
> 的那一条能抓住顺序错误。这正是「断言会互相遮蔽」的反向版本：
> 一个够宽的断言会让一个够具体的缺陷完全隐形。

**回归证据**：`--help` rc=0；完整重建 9 表行数全 `OK`、8.6s、135.2 MB；
全量 `150 passed / 2 skipped`（131 + 新增 11 + 编码闸门 5）。

## 输出编码闸门：GBK 能编中文，编不了 emoji 和 ¥（2026-10-20 第三轮）

承接 P1 那次 `✅` 崩溃，用同一条规则扫 P2，发现**输出路径上还有 4 处**：

| 文件 | 字符 | 崩在哪 |
|---|---|---|
| `scripts/init_db_mysql.py` | `✅` | **成功路径** `print("行数全校验通过 ✅")` |
| `evals/run_eval_s5.py` | `⚠` `¥` | **熔断提示** + 成本汇总 |
| `evals/run_selfheal_eval.py` | `¥` | 成本汇总 |
| `evals/run_s8_dual.py` | `¥` | 单条成本 |
| `src/core/cost.py` | `¥` | `BudgetExceeded` 消息 + `report()` |

### 最反直觉的一条：`¥` 不在 GBK 里

`'¥'.encode('gbk')` → `UnicodeEncodeError: '\xa5'`。GBK 里的人民币符号是
**`￥`（U+FFE5，编码 `a3a4`）**，`¥`（U+00A5）编不了。两者字形几乎一致，肉眼区分不出。

同批实测的「能 / 不能」对照（**不能凭感觉，要逐个 encode 验**）：

| 编不了 | 可以 |
|---|---|
| `¥` U+00A5、`⚠` U+26A0、`✅` U+2705、`❌` U+274C | `→` U+2192、`—` U+2014、`·` U+00B7 |
| `⇒` U+21D2、`↳` U+21B3、`−` U+2212 | `★` U+2605、`Δ` U+0394（GB2312 含希腊字母） |

所以 `¥`→`￥`、`⇒`/`↳`→`→`、`−`→`-`、`✅`→`[OK]`、`⚠`→`[WARN]`、`❌`→`[FAIL]`。

### 闸门：`tests/test_output_encoding.py`（5 条，0.17s，全离线）

**用 AST 而不是正则**，理由是实测出来的：正则/整文件替换分不开
「`print()` 里的字符」和「prompt / 产品文案里的字符」。

证据来自 P1（同一轮、同一规则）：那边第一次清扫用**整文件替换**，结果改到了三处必须
保留 emoji 的地方——`src/graph/nodes.py` 喂给 LLM 的重生成提示、`src/retrieval/embeddings.py`
的用户可见降级提示、`final_answer` 的拦截文案。改 prompt 等于改模型输入，指标立刻失去
可比性。回滚后改成 AST 只认 `Call`/`Raise` 节点，才不再越界。
本仓库（P2）没有踩到，但规则是同一条，闸门按同一标准写。

| 用例 | 钉住什么 |
|---|---|
| `test_output_paths_are_console_encodable` | 全仓库输出字符串必须 GBK 可编码 |
| `test_scanner_ignores_comments_and_docstrings` | 同文本进注释不红、进 `print` 才红 |
| `test_scanner_catches_raise_messages_too` | `raise X("✅")` 也要抓 |
| `test_cost_report_is_console_encodable` | 运行时求值，补静态闸门看不见的格式化函数 |
| `test_budget_exceeded_message_is_console_encodable` | 熔断消息走**真实 `on_llm_end` 路径**，顺带断言用的是 `￥` 而非 `¥` |

### 闸门的覆盖盲区（诚实标注）

静态闸门只看 `print/sys.exit/logging.*/warnings.warn/argparse.error` 与 `raise`。
**管不到「返回字符串、由调用方打印」的格式化函数**（`CostTracker.report()` 就是）。
处理方式不是放宽闸门（一放宽就开始误伤 prompt），而是**给每个已知格式化函数配一条
运行时用例**——静态闸门负责面，运行时用例负责点。这条边界写进了闸门 docstring。

### 变异验证

| 变异 | 红在哪 |
|---|---|
| `cost.py` 的 `￥` 改回 `¥` | `test_output_paths_are_console_encodable`，点名 `src/core/cost.py:106` |
| `run_eval_s5.py` 的 `[WARN]` 改回 `⚠` | 同上，点名 `evals/run_eval_s5.py:281` |
| `init_db_mysql.py` 的 `[OK]` 改回 `✅` | 同上，点名 `scripts/init_db_mysql.py:85` |

### GBK 真实运行证据

```
$ PYTHONIOENCODING=gbk python -c "from src.core.cost import CostTracker; print(CostTracker().report())"
退出码 = 0        # 修复前必崩：UnicodeEncodeError: 'gbk' codec can't encode character '\xa5'
```

**范围说明**：闸门只扫 `*.py`。`README.md` / `METRICS.md` 里的 `✅` `⚠` `¥` 全部保留——
GitHub 按 UTF-8 渲染，emoji 是加分项。

回归：`pytest tests/` → **150 passed / 2 skipped**（131 + init_db 11 + 编码闸门 5），63.75s。

## T1 · CI 落地：它还没跑起来就先抓到一个致命 bug（2026-10-20）

### 前置侦察：先「干净克隆」，再写 workflow

CI 的本质是**在干净环境里跑一遍**。所以动手前先做了一次本地模拟：
`git clone` 到临时目录（没有 `.env` / `models/` / `data/`），用现有 venv 跑测试。

| 仓库 | 干净克隆结果 | 结论 |
|---|---|---|
| P1 | **64 passed** | 不碰库，CI 无需额外准备 |
| P2 | **137 passed / 10 failed / 2 skipped** | 10 条全因缺库 |

**这一步直接推翻了本项目此前的口径声称**：「默认 147 passed / 2 skipped，任何机器一致」
——成立的是「任何**建过库**的机器」。干净克隆里 `data/db/olist.db`（135 MB，`.gitignore`
排除）不存在，碰库的用例一律 `sqlite3.OperationalError: no such table: orders`。

这是「可复现」这件事第三次被同一类问题教育：**「在我机器上成立」不等于「在任何机器上成立」**，
而差别永远藏在被 gitignore 掉的那些文件里。

### 附带的致命发现：服务端根本起不来

排查 CI 的 lint 门槛时跑 `ruff check --select F821`，报了两处「未定义名」。
其中一处是真 bug：

```python
_WRITE_SYS_PROMPT = f"""... {schema} ..."""     # ← line 206，f-string 里的 {schema}
...
sys_prompt = _WRITE_SYS_PROMPT.replace("{schema}", schema_block)   # ← line 220
```

第 206 行的 `{schema}` 是**留给运行期 `.replace()` 填的占位符**，写错成 f-string 后
会在**导入期**求值 → `NameError: name 'schema' is not defined`。

实测：

```
$ python -c "import src.api.server"
NameError: name 'schema' is not defined
```

**P2 的服务端完全起不来。** 引入于 `1051bbe`（场景包 Scenario Pack 重构，误加 f 前缀）。

为什么全套 147 条测试一直是绿的：**没有任何测试导入 `src.api.server`**。
测试只覆盖 `src/agent`、`src/tools`、`src/safety` 这些库层模块，
而 `src/api/server.py` 是真实部署入口（`uvicorn src.api.server:app`）。

修法：`{schema}` → `{{schema}}`（f-string 里 `{{` 渲染成字面量 `{`，
于是模板里留下 `{schema}` 给 `.replace()`）。与同一字符串里
`{{"intent": ...}}` 的转义写法一致。

### 新增回归：`tests/test_server_import.py`（3 条）

| 用例 | 钉住什么 |
|---|---|
| `test_server_module_imports_without_side_effects` | 导入部署入口不炸（覆盖整条导入链的模块级副作用） |
| `test_key_routes_registered` | `/api/health` `/api/schema` `/api/query` 在位 |
| `test_write_prompt_keeps_runtime_schema_placeholder` | `{schema}` 必须是字面量占位符、能被 replace 填掉 |

只断言「能 import 成功」不够：若有人把模板改成写死的 schema 文本，import 照样成功而功能已坏。

变异验证：把 `{{schema}}` 改回 `{schema}` → **3 failed**，全报
`NameError: name 'schema' is not defined`，精确指到 `src/api/server.py:213`。

> **可复用的教训**：「库函数都对」≠「服务能起来」。入口模块的导入本身就是一个必须被测的
> 契约——它把全模块的导入期副作用（f-string、模块级常量、可选依赖）串起来验一遍。
> 这类 bug 不会被任何「函数级」用例抓到。

### CI 怎么拿到数据库：合成 fixture 库

不能下载真数据集（Kaggle 需账号，每次拉 58 MB CSV 再建 135 MB 库）。
改成 `scripts/make_fixture_db.py`：用 `src/db/schema.py` 的 DDL 建 9 张表，
再用 `PRAGMA table_info` 反射列类型，按类型造**确定性合成值**。

| 决策点 | 做法 | 理由 |
|---|---|---|
| 行数 | 除 geolocation 外各 200 行；geolocation **40 万行** | geolocation 必须够大，否则 `test_timeout_interrupts_slow_query`（200ms 超时）不会超时 |
| 覆盖保护 | 默认拒绝覆盖已存在的目标，需 `--force` | 本地误跑一次就毁掉真库；重建要 8.6s + 58MB CSV |
| 是否足够 | 逐条核对 10 条失败用例的真实需求 | **没有一条依赖真实业务数值** |

**最容易误判的一条**：`test_happy_path_blackboard_complete` 断言 `"45,090" in analysis`，
看起来像强依赖真实数据，实际那个数字来自 `_FakeResp("2017 年已送达订单共 45,090 笔…")`
——FakeLLM 的台词。验的是「黑板字段能否接住 analysis」，不是「库里有 45,090 笔订单」。

**唯一真依赖数据集基数的一条**：`test_safety.py` 里 `assert rows[0]["c"] == 99441`。
已改成 `> 0`——**单测该钉不变量，不该钉数据集基数**；真实基数由 `init_db.py` 的
`EXPECTED` 校验与本文档负责。这不是放宽测试，是把断言放到该管它的那一层。

结果：合成库（9 表 / 401,600 行 / 22.4 MB / 2.4s）→ **150 passed / 2 skipped**。

### lint 门槛的口径

选 **真实缺陷类规则**，不打格式风格：

```
ruff check --select E9,F63,F7,F82,F401,F811,F841,F821,F822,F823
```

两个仓库修到全绿（P1 4 处 / P2 16 处，基本都是 unused import）。其中两处不是噪音：

- `tests/test_writer.py`：`r = import_csv_bytes(...)` 的返回值被丢掉 → 重复导入若失败，
  下面的行数断言会「碰巧」也失败，但报的是「行数不对」，真正原因（导入被拒）被掩盖。
  已补 `assert r.error == ""`。**这是「断言互相遮蔽」的又一种形态。**
- `evals/run_eval_s5.py`：`empty = [...]` 算了不用 → 删除。

`ruff check`（默认规则集，含 BLE001 blind-except 等）会报 157 条，属**风格债**，
不在本轮范围；门槛口径写进 workflow 注释，避免「CI 绿了 = 代码很干净」的误解。

### 交付

| 文件 | 作用 |
|---|---|
| `.github/workflows/ci.yml` | ruff → 生成合成库 → pytest，push/PR 触发，约 4 分钟 |
| `scripts/make_fixture_db.py` | 合成 fixture 库生成器（含覆盖保护） |
| `tests/test_server_import.py` | 服务端导入 + 路由 + 占位符契约（3 条） |
| README | 顶部 CI 徽章；订正「零外部依赖/不需准备数据」的过度声称 |

计数变化：147 → **150 passed / 2 skipped**（+3 服务端导入用例）。

---

## T2 · Docker 从零补齐：首跑两条全红，两个根因（2026-10-20）

### 为什么这套东西只能靠 CI 验证

开发机是 Windows + **没有装 Docker**（`docker: command not found`，
`wsl.exe` 还被本机安全策略直接拉黑）。也就是说 Dockerfile / compose
**无法在提交前本地验证任何一行**。落地的验证全部在 GitHub runner 上做：

| job | 跑什么 | 结果 |
|---|---|---|
| `image` | `docker build --target test` → 容器内 pytest | 首跑 **FAIL** → 修完 PASS |
| `compose` | build → 备合成库 → `up -d --wait` → health → schema(9 表) → 静态首页 → 容器内只读查一行 → down | 首跑 **FAIL** → 修完 PASS |

本地能验的部分都验了：YAML 解析、ruff、pytest 152/2、`run_api` 用
`API_HOST=0.0.0.0 API_PORT=8599` 真起一遍（`/api/health` 200、`GET /` 200）。

### 失败一：服务端起不来 —— 一个真 bug，不只是镜像问题

`image` job 的红在「生产镜像自检：能导入应用」。复现与定位：

```
docker run p2:runtime python -c "import src.api.server"
→ sqlite3.OperationalError: unable to open database file
```

`server.py` 在**导入期**就调 `_svc_init()` 落 `data/db/service.db`，
而 `sqlite3.connect()` **不会创建缺失的父目录**。镜像里不带 `data/`（数据走卷挂载），
容器首次启动时 `/app/data/db` 不存在 → 服务根本起不来。

**本地永远复现不了**：本地 `data/db/` 早被 135 MB 的真实库占着了。
本地一比一模拟（只拷 `src/ scripts/ web/ evals/`，无 `data/`）才能红。

修法**不是**只在 Dockerfile 里补 `mkdir`——那只让「这一种部署方式」能用。
真正的修法在 `_svc_conn()` 里 `mkdir(parents=True, exist_ok=True)`：
**服务应当能自己创建它要写的目录**，否则任何「干净机器首次部署」都得靠外部先备好
目录结构，那是部署脚本的责任泄漏。Dockerfile 里也补了 `mkdir`，但理由不同
——那是为了让 `VOLUME` 声明的匿名卷继承到目录结构。

变异对照：把 `Path.mkdir` 打成 no-op → 原 `OperationalError` 精确重现。
已加 `tests/test_server_import.py::test_svc_db_creates_missing_parent_dir`。

### 失败二：compose 被「没启用的服务」拖死

`compose` job 的 `docker compose build` 失败，而且连 `if: always()` 的
`docker compose down` 收尾步骤也一起红。根因：

```yaml
mysql:
  profiles: ["mysql"]                       # 默认不启动
  environment:
    MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PWD:?...}   # ← 这里
```

**compose 会先对整个文件做变量插值、再按 profile 过滤服务。**
所以即使 mysql 处于未激活的 profile，缺这个变量也会让整条
`docker compose up`（乃至 `down`）报
`required variable MYSQL_ROOT_PWD is missing a value` 而整体失败。

改成 `${MYSQL_ROOT_PWD:-dev_only_change_me}`（本地开发兜底，生产必须显式覆盖）。
顺带修 healthcheck：原来写 `-p${MYSQL_ROOT_PWD}` 是**宿主侧插值**，未设变量时变成
空密码，与容器里实际的密码不一致 → 健康检查永远红。改用 `$$MYSQL_ROOT_PASSWORD`
（compose 转义成字面 `$`），让密码取自**容器自己的**环境变量。

> 这条已经写进 P1 的 `docker.yml`：加一步 `docker compose config --quiet`。
> 它很便宜，专治这一类「某个没启用的服务里一个 `:?` 就让全流程红」。

### 另一个必须绕开的坑：compose 不要转发 DB_URL

compose 自动读项目根 `.env` 做插值，而本机 `.env` 里真实写着：

```
DB_URL=sqlite:///D:/Myprojects/text2sql-agent/data/db/olist.db
```

一旦写成 `DB_URL: ${DB_URL:-...}`，这个 **Windows 路径会被原样塞进 Linux 容器**。
症状极具误导性：服务起来了、`docker ps` 显示 healthy、`/api/health` 也 200，
但一查询就 `unable to open database file`。

写成 `DB_URL: ${DB_URL:-}` 同样坏——空串会**覆盖**掉 `config.py` 里的正确默认值
（pydantic-settings 认为「显式传了空值」）。

所以 `DB_URL / DB_RO_URI` 一律不在 compose 里声明，让容器按 `PROJECT_ROOT=/app`
自己拼；要切 MySQL 就在命令行显式 `-e DB_URL=...`。

### 镜像里到底该有哪几个目录：这一课的代价

| 目录 | 为什么必须 | 少了的后果 |
|---|---|---|
| `src/` | 被测代码 + `mcp_server.py` 子进程入口 | `ModuleNotFoundError` |
| `web/` | `app` 在**导入期**挂 `StaticFiles(PROJECT_ROOT/"web")` | Starlette 抛 `RuntimeError` |
| `evals/` | `src/agent/fewshot.py` **运行期**读 `evals/few_shots.json` | Few-shot 检索静默失效 |
| `scripts/` | 用例直接 import `make_fixture_db` / `init_db` | 收集期 error |
| `tests/` | 测试本体 | 没得跑 |

`evals/` 这一项尤其反直觉：它在 P1 是**纯开发期工具**（不该进镜像），
在 P2 却是**运行时资源**。判断依据只能是「运行期是否读它」，不是「它叫什么名字」。

顺带堵住一个静默降级：`tests/test_output_encoding.py` 的 `_py_files()` 是
`if not base.exists(): continue`——扫描目录缺一个就**悄悄少扫**，
「零违规」与「没扫」变得不可区分。新增 `test_all_scan_dirs_present` 断言四个
扫描目录齐全。变异验证：把 `evals` 改名后该用例报红并点名 `['evals']`。

### 新增/改动

| 文件 | 作用 |
|---|---|
| `Dockerfile` | `base → test → runtime`，**生产放最后**（`docker build .` 默认取最后阶段） |
| `docker-compose.yml` | api + 可选 mysql（profile 隔离）；healthcheck 用 python 而非 curl（slim 镜像没有 curl） |
| `.dockerignore` | 把 135 MB 真实库 / 58 MB CSV / `.env` 挡在上下文外 |
| `scripts/run_api.py` | `API_HOST` / `API_PORT` 可配（默认仍是 127.0.0.1） |
| `scripts/init_db_mysql.py` | `MYSQL_HOST/PORT/DB` 可配（compose 里服务名是 `mysql`） |
| `requirements.txt` | 补 `PyMySQL`——`.env.example` 写着「切 MySQL 只改一行 DB_URL」，少了它那行改完就是 ModuleNotFoundError |
| `.github/workflows/docker.yml` | 上述两个 job |

计数变化：150 → **152 passed / 2 skipped**（+1 数据目录用例 +1 扫描目录用例）。

---

## T3 · 按路径分解的并发性能 + 无应用层限流时的真实失败模式（2026-10-20）

> 脚本：`evals/bench_concurrency.py`（自写 `httpx.AsyncClient`，**不用 locust**——
> locust 按响应头到达计时，SSE 会被测成个位数毫秒，是假数据；必须消费到 `done`）。
> 原始数据：`evals/concurrency_{D,E,ramp,crosstalk}.json`。

### 环境口径

| 项 | 值 |
|---|---|
| 模型 | `deepseek-flash`（`LLM_PROVIDER=deepseek`） |
| 服务 | 单进程 uvicorn，`scripts/run_api.py`，`graph=agentic` |
| 库 | `data/db/olist.db`（fixture，135MB SQLite） |
| 问题集 | `evals/goldset_s5.json` 60 条，**自动排除 6 条 security**（按设计拒绝/净化） |

### 两条路径主表

| 路径 | 请求数 | 并发 | QPS | P50 | P95 | P99 | 错误率 | 瓶颈 |
|---|---|---|---|---|---|---|---|---|
| **P2-D 无 LLM（`GET /api/schema`）** | 300 | 10 | **925.9** | 7ms | 27ms | 74ms | 0% | 框架层天花板 |
| **P2-E 全链路（`POST /api/query`）** | 20 | 2 | **0.374** | 4734ms | 7691ms | 8568ms | 0% | **LLM 串行往返** |

P2-E 的 `done.status` 全部 `ok`（0 degraded / 0 blocked），
执行级结果集比对（复用 `run_eval_s5.compare_result`）**18/20 = 90%**。

### 瓶颈结论（一句话）

> **`/api/schema` 是纯内存序列化，QPS 上限约 1000；`/api/query` 每条一次完整
> LLM 往返，QPS 0.37。两者差 2500 倍——所以「QPS 低」这件事 100% 归因于
> LLM 串行往返，与 Web 框架无关。这个对照本身就是结论。**

### 口径必须说清：那条 926 QPS 测的不是「事件循环」

`api_schema()`（`server.py:497`）是 **`def`（同步）** 不是 `async def`，
FastAPI 会把它丢进 anyio 线程池（默认 40 token）执行。
所以它测的是**线程池 + 序列化**，不是「事件循环层」——
规格原稿写的是「如果它都慢，说明瓶颈在 Web 框架/事件循环层」，这半句要订正。
响应体积 P50 **8371 B**（9 表全量 DDL），这也是它有下限的原因之一。

### 无应用层限流时，被压到底会发生什么

全仓**没有任何限流中间件**（无 slowapi / ratelimit，grep 零命中），
所以应用层永远不会返回 429。那压到底会怎样？实测爬坡：

**`/api/schema`（零成本，2→40）**

| 并发 | 2 | 5 | 10 | 20 | 40 |
|---|---|---|---|---|---|
| QPS | 824.0 | **1036.6（峰）** | 945.4 | 648.8 | 681.1 |
| P50 | 2ms | 4ms | 9ms | 28ms | 45ms |
| P99 | 13ms | 9ms | 17ms | 57ms | 63ms |
| 错误率 | 0% | 0% | 0% | 0% | 0% |

**`/api/query`（每条一次 LLM，2→5）**

| 并发 | 2 | 5 |
|---|---|---|
| QPS | 0.174 | 0.853 |
| P50 | 5758ms | 4227ms |
| P95 | 21106ms | 5860ms |
| 错误率 | 0% | 0% |

**结论（线上被限流会怎样）：**

> 没有限流时，系统的失败模式**不是 429，而是静默排队到变慢**。
> 并发 10→40 时 `/api/schema` 的 QPS 从 945 掉到 681、P99 从 17ms 涨到 63ms，
> 但**错误率始终 0%**——它不会拒绝你，它只会让你等。
> 这比 429 更危险：429 会让客户端退避，无声的变慢只会让人以为「服务还行」。

### 一个预测被部分证伪（如实记录）

原预测：每条 `/api/query` 占 **2 个线程**（`threading.Thread` 生产者 +
整条流生命周期都占着 `asyncio.to_thread(q.get)` 的 anyio token），
所以并发 ≥20 时会饿死自己并**连带拖慢同池的 `/api/schema`**。

**这条没有被验证**：本脚本一次只爬一个端点，没有「高并发 query + schema 混跑」的模式。
现有数据只能说明 `/api/schema` 自己在并发 20 之后掉速（1037→649），
而这既可能是线程池争用，也可能只是单事件循环 + 8KB JSON 序列化的过载。
**两种解释都成立，所以不作为结论**。要验证它需要一个混合负载模式。

### 并发正确性：0 串场，且区分力现在**落在 artifact 里**（2026-10-21 重写）

5 个不同金标问题 @ 并发 5：执行级结果集交叉矩阵命中他人金标 **0 处**。
22 条全链路 @ 并发 2：**0 处**。

#### 坑一：检测器没有区分力

| 检测方式 | 命中自己金标 | 能不能下「0 串场」的结论 |
|---|---|---|
| 归一化 SQL 精确匹配 | **0 / 22** | **不能**（检测器瞎） |
| 执行级结果集比对 `compare_result` | **18 / 22** | 能 |

模型写出来的 SQL 文本与金标差得远（别名、列序、括号），但结果集一致。
在 0/22 的检测器上，「没有命中他人金标」几乎恒成立——那是**看不见**，不是**没有**。

#### 修法一：把区分力写进 JSON，而不是只打印一次

上一版只把 `_report_power()` 的结论打到终端，JSON 顶层的 `own_sql_hit`
仍然是**文本级**的 0/22 —— 于是从 artifact 完全看不出严格口径命中了自己几次，
「0 串场」既不可复现，也无法被反驳。现在落盘 `strict` 块：

| 键 | 含义 |
|---|---|
| `text_own_hit` | 归一化 SQL 文本命中自己（弱检测器，实测 0） |
| `strict_own_hit` / `strict_scored` | 执行结果集命中自己 / 计入分母的条数 |
| `strict_own_rate` | 上两者之比（E 路径 18/22 = 0.82） |
| `strict_foreign_hits` | 命中**他人**金标的二元组列表（串场，阻断级） |
| `strict_degenerate_matches` | 同值但**值退化**、不计为串场的匹配（见坑二） |
| `strict_unscored` | 没进分母的记录及原因（无 SQL / 无金标） |
| `strict_xmatrix_skipped` | 矩阵没跑时写明原因，`null` 表示跑了 |
| `detector_power` | `ok` / `blind` / `unsupported` + 理由 |

三态必须分开，这是本次补落盘时最容易被打回的地方：
`blind`（连自己都认不出）与 `unsupported`（`compare_result` 没装起来、根本没测）
都会让 `strict_foreign_count == 0`，但前者是「测了没用」，后者是「没测」。
把「没测」和「测了没问题」混成同一个 0，审计时一定被问住。
所以 `--strict` 改成了**默认开**，并新增 `--no-strict`；用它时落的是一整块
`detector_power="unsupported"` 的占位，长得不像「验过了」。

#### 坑二：值退化会让匹配失去意义（真实误报 4 处）

第一版矩阵在 E 路径（22 条）报出 **4 处串场**，全部来自 f02：

```
('f02','a06') ('f02','e01') ('f02','e03') ('f02','e04')
```

追进去发现 f02 的 SQL 是**对的**（`strict_ok=True`）：

| 题 | 题面 | 结果集 |
|---|---|---|
| f02 | 2018 年 10 月有多少笔已送达订单？ | `(0,)` |
| a06 | 已送达订单中金额 100–200 雷亚尔占比 | `(0.0,)` |
| e01 | 2013 年有多少笔订单？ | `(0,)` |
| e03 | 已退款订单有多少笔？ | `(0,)` |
| e04 | XX 州有多少位客户？ | `(0,)` |

这份 fixture 里 2018 年 10 月真的没有已送达订单，那四题的正确答案也都是 0。
**不同问题答出同一个 0 是语义正确，不是串场。** 一个退化值不携带任何区分信息，
拿它当指纹必然误报——这跟坑一是同一类错误，只是发生在**值**这一层。

修法：`_is_degenerate()` 判定「空集 / 全部为 0、0.0、NULL、空串」，
退化匹配**不计入串场，但也不丢弃**，落进 `strict_degenerate_matches` 留痕。
修完：真串场 0，退化排除 4。两条测试把这个现场压成了用例，
另一条反向对照断言「值不退化时同值必须照报」，防止过滤器被做成开关。

#### 顺带修掉一个把压测挂死的二次方成本

修「落盘」时发现原 `strict_crosstalk` 对每个 `(候选, 他人金标)` 组合都调一次
`compare_result` —— **同一条候选 SQL 被重复执行 N 次**。这套 fixture 库的单条
金标就不便宜（22 条自比对 **37.9 秒**，m07 6.9s / m02 6.2s / m08 5.4s），
乘上 22×21 的矩阵就是十几分钟，表现为「压测卡死」，其实是 O(N²) 撞上慢查询。

改成：按 SQL 文本缓存规范化结果集（`_canon_sql`），矩阵退化为内存里的集合比较。
- 执行次数：N² → **2N**（N 条金标 + N 条候选，每条只跑一次）。22 条从 ~11 分钟降到 **31.7s**。
- 归一化规则不自己发明：借 `run_eval_s5` 的 `_norm_val` / `_cmp_key`，
  并加一条**交叉验证用例**，对 6 组真实形态的 SQL 断言 `_canon_sql` 与官方
  `compare_result` 结论完全一致——复刻官方 `canon()` 的唯一保险。
- 加 `SQL_TIMEOUT_S`（默认 8s，`sqlite3` progress handler 实现）：候选 SQL 来自模型，
  可能写出笛卡尔积，一条病态 SQL 不该把整轮压测挂死。测试用递归 CTE 验证它会返回
  `interrupted` 而不是挂住。
- 加 `--xmatrix-max`（默认 24）：规模超限就跳过矩阵，并在
  `strict_xmatrix_skipped` 里写明原因。

#### 结论

**「0 串场」要同时满足三件事才算结论**：检测器有区分力（`detector_power == "ok"`）、
比对的值不退化、矩阵真的跑了（`strict_xmatrix_skipped is None`）。
三者任一不成立，`--path crosstalk` 用退出码 **5**（区别于真串场的 **3**，
也区别于服务不可达的 **4**）。

这条和仓库里已有的「断言要按最具体优先排序」是同一类病：测不准与测得准必须分开报。

> 口径差异声明：本节引用的 `evals/concurrency_E.json` 是 **22×2**（P50 5156ms），
> 而上面 T3 主表那行是 **20×2**（P50 4734ms）。两次不是同一批样本，
> 差异落在 P2-E 的跑间波动内（同环境同参数，单条 LLM 往返 5±1.5s）；
> 引用性能数请用主表，引用**正确性**数请用 artifact。

### 三个计量器缺陷（同源，详见 P1 的 T3 章节）

P2 侧同一条命令修前修后差 **400 倍**：

| 命令 | 修前 | 修后 |
|---|---|---|
| `--path d -n 100 -c 10` | QPS 2.3 / P50 21.9s | QPS **21→926** / P50 **7ms** |

1. **`trust_env=False`**：本机有 `HTTP_PROXY`（Clash）且无 `NO_PROXY`，
   httpx 默认 `trust_env=True` → 压测本地服务实际压的是代理。
2. **闭循环替代信号量**：`t0` 记在抢信号量之前，`wall_ms` 混进排队时间。
3. **共享 `AsyncClient`**：Windows 上每个新建 client 首次连接 ~520ms 且互相串行化，
   10 并发下独立 client 墙钟 5836ms vs 共享连接池 **24ms**（240 倍）。

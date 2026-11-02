# Text2SQL Agent —— 镜像（T2，2026-10-20）
#
# 阶段顺序很要紧：**生产必须是最后一个阶段**。`docker build .` 与
# `docker compose build` 不带 --target 时构建的是最后一个阶段；若把 CI 测试阶段
# 放最后，生产镜像会变成「带着 tests/ 和合成库」的测试镜像。
#
#   docker build .                       → runtime（生产，默认）
#   docker build --target test -t t .    → test（CI 在容器内跑 pytest）
#
# ## 为什么数据不进镜像
#
# `data/`（真实库 135 MB + 原始 CSV 58 MB）走卷挂载。镜像只含代码与依赖：
# 换数据集不用重建镜像，重建镜像不用重新下载数据。
#
# ## 为什么 test 阶段要自己生成合成库
#
# 本项目有 10 条用例真的碰库（`data/db/olist.db`，见 scripts/make_fixture_db.py 的
# docstring）。若 test 阶段不备库，`docker run --rm p2:test` 会 10 条全红。
# 在**构建期**用 schema.py 的 DDL 反射生成合成库，好处是「一条 docker run 就自洽」，
# 不依赖宿主机先建好库、也不用挂卷。代价是多花约 10s 构建时间，换来可重复性。

# ---------- 阶段 1：依赖（生产与测试共用）----------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先只拷 requirements 再装依赖：代码改动不触发依赖重装，层缓存命中率最高。
# 注意 P2 的 requirements.txt 不含 torch（SQL Agent 不需要本地向量模型），
# 所以这里没有 P1 那个「单索引装 CPU torch」的步骤。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---------- 阶段 2：CI 测试（不进生产镜像）----------
# 必须拷齐：src/（被测代码 + mcp_server.py 子进程入口）、scripts/（make_fixture_db
# 与 init_db，有用例直接 import）、evals/（编码闸门会 AST 扫 evals/*.py，
# tests/test_output_encoding.py 的 _py_files() 在目录不存在时是 `continue`——
# 少了它闸门会**静默缩小扫描面**而不是报错）、web/（app 在导入期挂 StaticFiles）、
# tests/。
#
# 这一串拷法在 P1 上真摔过：test 阶段只拷 tests/，结果镜像能建、容器内 pytest
# 一起手就 ModuleNotFoundError。所以这里宁可多拷。
FROM base AS test

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY evals/ ./evals/

# 构建期生成合成 fixture 库（表结构真实、行数合成），让容器内的 pytest 自洽
RUN python scripts/make_fixture_db.py

COPY web/ ./web/
COPY tests/ ./tests/

CMD ["python", "-m", "pytest", "-q"]

# ---------- 阶段 3：生产镜像（默认 target）----------
FROM base AS runtime

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY web/ ./web/
# evals/few_shots.json 是**运行期资源**（src/agent/fewshot.py 读它做 Few-shot 检索），
# 不是纯评测产物，必须进镜像。整个 evals/ 只有 412 KB，顺带让容器内也能跑评测。
COPY evals/ ./evals/

# 容器内必须监听 0.0.0.0。scripts/run_api.py 的默认值刻意是 127.0.0.1
# （本机开发不需要对外开端口），照默认值跑会「docker ps 正常但宿主机连不上」——
# 那不是端口映射的问题，是进程只绑了容器内的 loopback。
ENV API_HOST=0.0.0.0 \
    API_PORT=8501

# 预建 data/db：一是下面 VOLUME 声明的匿名卷会继承镜像里已有的目录结构
# （空目录也继承），二是让「不挂卷直接 run」也能起来。
# 注意这只是兜底——真正的修复在 src/api/server.py 的 _svc_conn()：
# 服务必须能自己创建自己要写的数据目录，不该指望外部先准备好。
RUN mkdir -p /app/data/db

EXPOSE 8501

# 数据卷挂载点：真实库、CSV、审计日志、自定义表注册都在这里
VOLUME ["/app/data"]

CMD ["python", "scripts/run_api.py"]

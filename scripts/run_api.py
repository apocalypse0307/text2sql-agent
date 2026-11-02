"""P2-S6 API 启动入口（FastAPI + SSE，默认端口 8501）。

运行：python scripts/run_api.py
注意：LLM Key（AGNES_API_KEY / DEEPSEEK_API_KEY）从环境变量或项目根 .env 读取，
     缺失时服务能启动，但发起问答会报 OpenAIError: Missing credentials。

容器内务必设 API_HOST=0.0.0.0：默认值刻意保留 127.0.0.1（本机开发不需要对外监听，
默认对外开端口是没必要的暴露面）。容器里若还是 127.0.0.1，宿主机端口映射会连不上——
这不是端口没映射，而是进程只绑了容器内的 loopback，`docker ps` 显示正常但请求被拒。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

# 保证 uvicorn 能 import 到 src 包（直接 python scripts/run_api.py 时 sys.path[0]=scripts）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST = os.getenv("API_HOST", "127.0.0.1")
PORT = int(os.getenv("API_PORT", "8501"))

if __name__ == "__main__":
    uvicorn.run("src.api.server:app", host=HOST, port=PORT, reload=False)

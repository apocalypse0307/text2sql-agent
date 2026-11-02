"""
服务端「能否起来」的冒烟测试（P2，2026-10-20）。

## 为什么需要它

`src/api/server.py` 是**真实部署入口**（`uvicorn src.api.server:app`），
但在加本文件之前，**没有任何测试导入过它**。

后果已经发生过一次：场景包重构（commit `1051bbe`）给 `_WRITE_SYS_PROMPT` 误加了
`f` 前缀，而模板里有 `{schema}` 这个**本该留给运行期 `.replace()` 填**的占位符。
f-string 会在**导入期**求值 → `NameError: name 'schema' is not defined`，
整个 `src.api.server` 无法导入、服务端起不来。

而当时全套 147 条测试**全绿**——因为测试只测 `src/agent`、`src/tools`、`src/safety`
这些库层模块，一条都没走部署入口。

**教训**：「库函数都对」≠「服务能起来」。入口模块的导入本身就是一个必须被测的契约，
因为它把全模块的导入期副作用（f-string、模块级常量、可选依赖）串起来了。

## 覆盖

1. 导入不炸（含不依赖凭据/DB/网络——LLM 是懒创建，导入期不该连外部）
2. FastAPI app 与关键路由存在
3. `{schema}` 占位符契约：模板里是字面量、能被 `.replace()` 填掉
4. 数据目录不存在时也能起来（服务自己建目录，不指望部署脚本先准备好）

零外部依赖，不需要 key / DB / 模型。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_server_module_imports_without_side_effects():
    """导入部署入口不炸，且导出 app。

    这条看起来"什么也没测"，但它覆盖的是**整条导入链**：任何一个模块级
    表达式写错（f-string 提前求值、常量引用未定义名、可选依赖缺失）都会红在这。
    """
    import src.api.server as server

    assert hasattr(server, "app")
    # FastAPI 的 app 必须有路由；直接断言类型太脆，断言数量下限更稳
    assert len(server.app.routes) >= 10


def test_key_routes_registered():
    """关键路由在位——防止重构时把接口删了/改了前缀而没人发现。"""
    import src.api.server as server

    paths = {getattr(r, "path", "") for r in server.app.routes}
    for expected in ("/api/health", "/api/schema", "/api/query"):
        assert expected in paths, f"缺少路由 {expected}；现有：{sorted(paths)}"


def test_write_prompt_keeps_runtime_schema_placeholder():
    """`{schema}` 必须是**留在模板里的字面量**，由运行期 `.replace()` 填。

    这是上面那次 NameError 的直接回归测试。两个方向都断言：
      · 模板里 `{schema}` 还在（否则 build_write_proposal 的 replace 什么也换不到）
      · f-string 的其它占位符已求值（`{_PROFILE...}` 不该以原文残留）

    只断言「能 import 成功」不够：把 `{schema}` 改成 `{{schema}}` 虽然不再 NameError，
    但若有人改成写死的 schema 文本、删掉占位符，import 照样成功而功能已坏。
    """
    import src.api.server as server

    prompt = server._WRITE_SYS_PROMPT
    assert "{schema}" in prompt, "占位符丢了：build_write_proposal 的 replace 将静默失效"
    assert "{_PROFILE" not in prompt, "f-string 未求值，说明 f 前缀被去掉了"

    filled = prompt.replace("{schema}", "<SCHEMA_BLOCK>")
    assert "<SCHEMA_BLOCK>" in filled and "{schema}" not in filled


def test_svc_db_creates_missing_parent_dir(tmp_path, monkeypatch):
    """服务必须能自己创建它要写的数据目录。

    起因（Docker 首次真跑时炸的）：`server.py` 在**导入期**就调用 `_svc_init()`
    落 `data/db/service.db`。而 `sqlite3.connect()` 不会创建缺失的父目录，
    只会抛 `OperationalError: unable to open database file`。

    镜像里不带 `data/`（数据走卷挂载），容器首次启动时 `/app/data/db` 不存在
    → `import src.api.server` 直接失败、服务根本起不来。
    **本地永远复现不了**：本地 `data/db/` 早被 135 MB 的真实库占着了。

    所以断言点不是「连得上库」，而是「在目录不存在时也能连上，并把目录建出来」。
    """
    import sqlite3

    import src.api.server as server

    target = tmp_path / "not" / "yet" / "there" / "service.db"
    assert not target.parent.exists(), "前置条件：父目录一开始必须不存在"

    monkeypatch.setattr(server, "_SVC_DB", target)
    server._svc_init()

    assert target.exists(), "没有建出 service.db"
    con = sqlite3.connect(target)
    try:
        names = {r[0] for r in
                 con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    missing = {"history", "feedback", "proposals"} - names
    assert not missing, f"建表不全，缺：{missing}"

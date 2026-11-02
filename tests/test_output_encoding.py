"""
运行时输出编码闸门（P2，2026-10-20）。

## 为什么需要它

Windows 控制台默认 cp936(GBK)。GBK **能**编码中文，却**不能**编码这些常用符号：

| 字符 | 码位 | GBK | 替代 |
|---|---|---|---|
| ✅ | U+2705 | 编不了 | `[OK]` |
| ⚠ | U+26A0 | 编不了 | `[WARN]` |
| ¥ | U+00A5 | **编不了** | `￥`（U+FFE5，GBK 原生 `a3a4`，同形） |

后果不是乱码，是 **`UnicodeEncodeError` 直接把进程打死**——而且往往死在
「结论行」「成功提示」「熔断提示」这种最需要被看见的位置。看起来像业务出错，
实际是 IO 契约出错。

## 已发生的事故（都真踩过）

- P1 `scripts/verify_models.py`：两个模型都验证通过了，崩在最后一行打印 `✅ 全部通过` 上
- P2 `scripts/init_db_mysql.py`：**成功路径** `print("行数全校验通过 ✅")`
- P2 `evals/run_eval_s5.py`：**熔断提示** `print("⚠ 本轮因超预算熔断…")`
- P2 `src/core/cost.py`：`BudgetExceeded` 与 `report()` 里的 `¥`（反直觉：以为 ¥ 肯定在 GBK 里）

## 为什么用 AST 而不是正则扫行

正则扫行会把**注释和文档字符串里提到**的 `print("✅")` 也算成违规（本仓库里就有这种
段落），只能靠 allowlist 打补丁，而 allowlist 迟早会烂。AST 只认真正的 `Call` / `Raise`
节点，注释与 docstring 天然被排除，不需要任何白名单。

纯静态扫描，不 import 被扫模块（避免副作用与慢加载）。零外部依赖。
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCAN_DIRS = ("src", "scripts", "evals", "tests")
SKIP_PARTS = {".venv", "venv", "__pycache__", ".tmp", ".git", "node_modules"}

# 会把字符串写到 stdout/stderr 的函数名（取 Name.id 或 Attribute.attr，
# 所以 print / sys.exit / logging.warning / warnings.warn / ap.error 都会被认到）
OUTPUT_FUNCS = {
    "print", "exit", "error", "warn", "warning",
    "info", "debug", "critical", "exception", "write",
}

# Windows 控制台默认代码页；换机器若用其它 ANSI 代码页，这里跟着改
CONSOLE_ENCODING = "gbk"


def _py_files():
    """列出要扫的文件（排除虚拟环境/缓存目录）。"""
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if SKIP_PARTS & set(p.parts):
                continue
            yield p


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _string_constants(node) -> list[ast.Constant]:
    """递归取出节点下所有字符串字面量（含 f-string 的固定片段）。"""
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _unencodable(text: str) -> list[tuple[str, str]]:
    """返回 text 里 CONSOLE_ENCODING 编不了的字符 [(char, 'U+XXXX')]。"""
    bad = []
    for ch in text:
        if ord(ch) < 128:
            continue
        try:
            ch.encode(CONSOLE_ENCODING)
        except UnicodeEncodeError:
            bad.append((ch, f"U+{ord(ch):04X}"))
    return bad


# ---------------- 扫描核心（抽成函数，便于用临时文件做「不误报」的证明）----------------
def collect_offenders(paths) -> list[str]:
    """扫给定文件，返回所有「输出路径上含不可编码字符」的描述行。

    一次收集**全部**违规再返回，不在第一个违规处中断——修一个跑一次的体验问题，
    本轮在 init_db 那边刚被指出过。
    """
    offenders: list[str] = []

    for path in paths:
        path = Path(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:          # 临时目录里的文件，不在仓库内
            rel = path.name

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in OUTPUT_FUNCS:
                targets = list(node.args) + [kw.value for kw in node.keywords]
            elif isinstance(node, ast.Raise) and node.exc is not None:
                targets = [node.exc]
            else:
                continue

            for target in targets:
                for const in _string_constants(target):
                    bad = _unencodable(const.value)
                    if not bad:
                        continue
                    ch, cp = bad[0]
                    snippet = const.value.strip().replace("\n", " ")[:60]
                    offenders.append(
                        f"  {rel}:{const.lineno}  {ch!r} {cp}  →  {snippet!r}")

    return offenders


# ---------------- 主闸门 ----------------
def test_output_paths_are_console_encodable():
    """所有「会打印出去」的字符串，都不许含 cp936 编不了的字符。

    覆盖两类出口，因为它们最终都到同一个控制台：
      · `ast.Call`  —— print / sys.exit / logging.* / warnings.warn / argparse.error
      · `ast.Raise` —— 异常消息也会被打印（traceback，或 except 里 `str(e)`）
    """
    offenders = collect_offenders(_py_files())
    assert not offenders, (
        f"以下运行时输出含 {CONSOLE_ENCODING} 无法编码的字符，"
        f"在 Windows 控制台上会抛 UnicodeEncodeError（进程直接死，不是乱码）：\n"
        + "\n".join(offenders)
        + "\n\n改法：状态标记用 ASCII（[OK]/[WARN]/[FAIL]）；货币用 ￥(U+FFE5) 而非 ¥(U+00A5)。"
    )


def test_all_scan_dirs_present():
    """扫描目录缺一个就**静默缩面**——这种降级必须报错，不能悄悄少扫。

    起因：`_py_files()` 里是 `if not base.exists(): continue`。Docker 的 test
    阶段若忘了 `COPY evals/`，闸门会照常变绿，只是少扫了几百个文件——
    于是「零违规」与「没扫」变得不可区分。在镜像里尤其危险：那里没有
    任何人能肉眼发现少了一个目录。

    这不是假想：P1 的 docker.yml 首次真跑就红在「test 阶段少拷目录」上
    （缺 `src/` 与 `web/`）。那次侥幸是 ModuleNotFoundError 叫出来的；
    若缺的恰好是 `evals/`，就没人叫了。

    **断言「检查本身发生了」，而不只是断言「检查通过了」。**
    """
    missing = [d for d in SCAN_DIRS if not (ROOT / d).is_dir()]
    assert not missing, (
        f"这些扫描目录不存在，编码闸门会静默缩小扫描面：{missing}"
        f"（根目录：{ROOT}）"
    )


# ---------------- 「为什么用 AST」的机器证明 ----------------
def test_scanner_ignores_comments_and_docstrings(tmp_path):
    """注释/docstring 里出现 `print("✅")` **不算**违规——这是 AST 相对正则的核心优势。

    没有这条，「用 AST 而不是正则」就只是一句自述。真正的门是：
    **同样的文本放进注释不红、放进 print 才红**。两个方向都断言，才算钉住。

    （本仓库里确实有这种段落：test_init_db.py 的 docstring 就写了 `print("✅")`
    当作事故举例。正则版会被自己的例子绊倒，然后只能加 allowlist，而 allowlist 会烂。）
    """
    prose_only = tmp_path / "prose_only.py"
    prose_only.write_text(
        '"""模块 docstring 里提到 print("\u2705") 是违规示例，但这里本身不是调用。"""\n'
        "# 注释里写 print('\u26a0 熔断') 也不该被判违规\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    assert collect_offenders([prose_only]) == []

    real_call = tmp_path / "real_call.py"
    real_call.write_text(
        'print("\u2705 全部通过")\n',
        encoding="utf-8",
    )
    offenders = collect_offenders([real_call])
    assert len(offenders) == 1
    assert "U+2705" in offenders[0]
    assert "real_call.py:1" in offenders[0]


def test_scanner_catches_raise_messages_too(tmp_path):
    """异常消息里的 emoji 也要抓——它最终同样会打到控制台。

    `raise X("✅")` 比 print 更险：平时不出现，只在出事时才现身，
    而「出事」正是最需要看清报错的时候。
    """
    f = tmp_path / "raiser.py"
    f.write_text('raise RuntimeError("\u2705 完成")\n', encoding="utf-8")
    offenders = collect_offenders([f])
    assert len(offenders) == 1 and "U+2705" in offenders[0]


# ---------------- 真实行为：成本报告必须能打到 GBK 控制台 ----------------
def test_cost_report_is_console_encodable():
    """`CostTracker.report()` 是给人看的收尾行，必须能在中文控制台打印。

    这条不只是重复主闸门：它走的是**真实 f-string 求值结果**，而不是源码里的字面量。
    若哪天 report() 改成从配置读货币符号，主闸门（静态扫源码）就看不见了，这条仍能抓。
    """
    from src.core.cost import CostTracker

    text = CostTracker().report()
    assert "实测成本" in text
    text.encode(CONSOLE_ENCODING)          # 编不了会抛，即失败


def test_budget_exceeded_message_is_console_encodable():
    """熔断异常消息同样要能打印——它是最需要被看见的那条。

    构造方式：把 cap 设为负数，`on_llm_end` 一旦累计出正成本就必然越界。
    用真实回调路径而不是手拼异常，才能同时验证「消息里用的是 ￥ 而非 ¥」。
    """
    from src.core.cost import BudgetExceeded, CostTracker

    class _Usage:
        """最小 usage 桩：满足 _extract_usage 的读取形态。"""
        def __init__(self):
            self.usage_metadata = {"input_tokens": 1000, "output_tokens": 1000,
                                   "input_token_details": {"cache_read": 0}}

    class _Resp:
        def __init__(self):
            self.llm_output = {}
            self.generations = [[type("G", (), {"message": _Usage()})()]]

    t = CostTracker(cap_cny=-1.0)
    with pytest.raises(BudgetExceeded) as ei:
        t.on_llm_end(_Resp())
    str(ei.value).encode(CONSOLE_ENCODING)

    assert "￥" in str(ei.value), "熔断消息应使用 GBK 可编码的 ￥(U+FFE5)，而不是 ¥(U+00A5)"
    assert "¥" not in str(ei.value)

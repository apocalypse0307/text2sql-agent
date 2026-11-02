"""
建库脚本 init_db.py 的前置检查套件（P2 加固，2026-10-20）。

为什么值得单开一个文件：这条 bug 的形态是「**缺数据时先删旧库再崩**」——
它不是功能错误，是**错误路径上的数据损失**。功能用例全绿也挡不住它，
只能靠一条专门钉住「顺序」的用例。

覆盖：
  ① 一次列全     —— 缺 9 个就报 9 个，不读一张崩一张（不短路）
  ② geolocation 特例 —— csv 缺但原 zip 在 = 不算缺失（脚本会自动解压）
  ③ 结构化载荷   —— 异常带 missing / raw_dir，调用方可程序化处理
  ④ **旧库不丢** —— 缺数据时抛异常，已有库文件字节不变（本条是核心）
  ⑤ 入口契约     —— 子进程退出码 2 + stderr 有人话（Kaggle 链接 + 目录）
  ⑥ 文案可编码   —— 报错文案能过 cp936（Windows 控制台默认 GBK）
  ⑦ 无数据不误伤 —— --dump-schema 只刷新文档，不该被前置检查拦住

零外部依赖：不读真实 data/raw、不联网、不调 LLM，任何机器结果一致。

运行：.venv/Scripts/python.exe -m pytest tests/test_init_db.py -v
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.init_db as init_db  # noqa: E402  （命名空间包，无需 __init__.py）
from scripts.init_db import (  # noqa: E402
    CSV_MAP,
    GEO_CSV,
    GEO_ZIP,
    KAGGLE_URL,
    MissingRawDataError,
    check_raw_data,
    require_raw_data,
)


# ---------------- ① 一次列全，不短路 ----------------
def test_check_raw_data_lists_all_missing_at_once(tmp_path):
    """空目录 → 9 个全报。

    这条钉的是「不短路」：若有人把实现改成遇到第一个缺失就 `return`，
    用户就会陷入「补一个 → 再跑一次 → 发现下一个」的循环，9 个文件跑 9 轮。
    所以断言的是**数量与集合**，不是「至少包含某一个」。
    """
    missing = check_raw_data(tmp_path)
    assert len(missing) == len(CSV_MAP) == 9
    assert set(missing) == set(CSV_MAP)


def test_check_raw_data_returns_empty_when_all_present(tmp_path):
    """9 个 CSV 都在 → 返回空列表（且不抛）。"""
    for name in CSV_MAP:
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    assert check_raw_data(tmp_path) == []


# ---------------- ② geolocation 的 zip 特例 ----------------
def test_check_raw_data_accepts_geolocation_zip(tmp_path):
    """只有 zip、没有 csv → geolocation **不算**缺失（rebuild_db 会解压）。

    不这么写的话，README 里明说「或放原 zip 也行」的用法会被自己的前置检查打死。
    """
    for name in CSV_MAP:
        if name != GEO_CSV:
            (tmp_path / name).write_text("x\n", encoding="utf-8")
    (tmp_path / GEO_ZIP).write_bytes(b"PK\x03\x04")   # 内容无关，只看存在性

    assert check_raw_data(tmp_path) == []


def test_check_raw_data_still_reports_geolocation_when_neither_present(tmp_path):
    """csv 和 zip 都没有 → 该报还是得报（别把特例写成跳过一切）。"""
    for name in CSV_MAP:
        if name != GEO_CSV:
            (tmp_path / name).write_text("x\n", encoding="utf-8")

    assert check_raw_data(tmp_path) == [GEO_CSV]


# ---------------- ③ 结构化载荷 + 人话文案 ----------------
def test_exception_carries_structured_payload(tmp_path):
    """异常带 missing / raw_dir：调用方能程序化处理，不必正则解析人话。

    同时断言文案三要素——缺几个、去哪儿下、放哪里。
    断言写成「包含 KAGGLE_URL 这个常量本身」而不是把 URL 抄一遍：
    后者一旦换数据源，测试就成了阻碍而不是保护。
    """
    with pytest.raises(MissingRawDataError) as ei:
        require_raw_data(tmp_path)

    exc = ei.value
    assert isinstance(exc.missing, list) and len(exc.missing) == 9
    assert Path(exc.raw_dir) == tmp_path

    msg = str(exc)
    assert "9" in msg                     # 缺几个
    assert KAGGLE_URL in msg              # 去哪儿下
    assert str(tmp_path) in msg           # 放哪里
    assert "init_db.py" in msg            # 放好后怎么跑


def test_exception_is_a_FileNotFoundError(tmp_path):
    """继承 FileNotFoundError：老调用方若写了 `except FileNotFoundError`，加固后不漏接。"""
    with pytest.raises(FileNotFoundError):
        require_raw_data(tmp_path)


# ---------------- ⑥ 报错文案必须能过 GBK ----------------
def test_error_message_is_cp936_encodable(tmp_path):
    """报错文案不能含 cp936(GBK) 编不了的字符（emoji 类）。

    刚在 P1 的 verify_models.py 上踩过：Windows 控制台默认 GBK，
    `print("✅")` 直接 UnicodeEncodeError。**报错文案自己崩掉**是最坏的情况——
    用户本来正需要看这段字。

    这条用例的存在意义是给文案加一道机器闸门：以后谁想往这里加 emoji，测试直接红。
    """
    with pytest.raises(MissingRawDataError) as ei:
        require_raw_data(tmp_path)
    str(ei.value).encode("gbk")      # 不抛 UnsupportedOperation 即通过


# ---------------- ④ 核心：缺数据不许动已有库 ----------------
def test_missing_data_does_not_delete_existing_db(tmp_path):
    """**本条是整个文件的核心。**

    原实现 `rebuild_db()` 的顺序是：解压 → `DB.unlink()` → 逐表 read_csv。
    数据不全时会在删库之后崩，把「少下载一个文件」升级成「已有数据库没了」。
    修法不是 try/except，而是把检查**移到 unlink 之前**——顺序即正确性。

    断言的是「字节完全不变」而不是「文件还在」：后者挡不住「先删了又建了个空壳」。
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    db_path = tmp_path / "db" / "olist.db"
    db_path.parent.mkdir(parents=True)
    sentinel = b"SQLite format 3\x00 existing-good-db"
    db_path.write_bytes(sentinel)

    with pytest.raises(MissingRawDataError):
        init_db.rebuild_db(raw_dir=raw_dir, db_path=db_path)

    assert db_path.exists(), "缺数据时把已有的库删掉了——检查没排在 unlink 之前"
    assert db_path.read_bytes() == sentinel, "库文件被改写/截断了"


# ---------------- ⑤ 入口契约：退出码 + 人话 ----------------
def _run_cli(tmp_path, argv=()):
    """起真子进程跑 `_cli()`，把 RAW/DB 指到 tmp（不碰真实数据）。

    为什么用 `-c` 而不是 `-m`：需要在**子进程内**改写模块常量，
    命令行跑真实脚本没法注入临时目录。用 `-c` 既保留「真进程 + 真退出码」这个
    外部契约，又能把数据目录指到 tmp。
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "db" / "olist.db"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "import scripts.init_db as m\n"
        f"m.RAW = Path({str(raw_dir)!r})\n"
        f"m.DB = Path({str(db_path)!r})\n"
        f"sys.argv = ['init_db.py', *{list(argv)!r}]\n"
        "m._cli()\n"
    )
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=120)


def test_cli_exits_2_and_points_to_kaggle_when_data_missing(tmp_path):
    """缺数据 → 退出码 2 + stderr 有 Kaggle 链接与目标目录。

    断言的是**外部契约**（退出码 + 人话），不是「抛了 MissingRawDataError」——
    后者是内部实现细节，将来换实现方式不该让这条测试红。
    退出码选 2 的理由写在 `_cli()` 注释里：1 已被「凭据缺失」占用。
    """
    proc = _run_cli(tmp_path)

    assert proc.returncode == 2, f"期望退出码 2，实际 {proc.returncode}"
    assert KAGGLE_URL in proc.stderr
    assert "data" in proc.stderr          # 目标目录的线索
    assert proc.stdout == "", "缺数据时的指引应走 stderr，stdout 保持干净"


def test_cli_help_still_works(tmp_path):
    """--help 不能被前置检查拦住（回归保护：检查只该在 rebuild 路径上）。"""
    proc = _run_cli(tmp_path, argv=("--help",))
    assert proc.returncode == 0
    assert "usage" in proc.stdout.lower()


# ---------------- ⑦ --dump-schema 无数据也不该失败 ----------------
def test_dump_schema_path_does_not_require_raw_data(tmp_path, monkeypatch):
    """`--dump-schema` 只刷新文档，不碰 raw 数据。

    这条防的是「把检查提到 main() 外层」这种过度加固：那会让只读文档的路径
    在没数据的机器上也失败（CI 里生成 schema.md 不该需要 58MB 数据集）。
    库不存在时 dump_schema 自己会跳过并返回 None。
    """
    monkeypatch.setattr(init_db, "RAW", tmp_path / "raw")          # 空目录
    monkeypatch.setattr(init_db, "DB", tmp_path / "db" / "none.db")  # 库不存在
    monkeypatch.setattr(init_db, "SCHEMA_MD", tmp_path / "schema.md")

    init_db.main(rebuild=False, dump=True)   # 不抛即通过

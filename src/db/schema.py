"""
Olist 数据库 Schema 定义（表名 / 字段类型 / 中文注释 / 采样值）。

语法：纯 dict 常量，DDL 用 SQLite 方言（dev 库）；换 MySQL 只改连接串，DDL 类型几乎一一对应。
原理：
  - 「注释即知识」是 Text2SQL 的免费午餐——每个字段的中文注释（含枚举值含义）直接决定
    LLM 选表选列的准确率。这份定义同时服务于：
    ① scripts/init_db.py 建表
    ② 给人读的 data/schema.md（本模块 render_schema_md 生成，init_db.py --dump-schema 刷新）
    ③ 给 LLM 的 prompt 注入（agent/prompting.render_schema_block，P2-S2 Schema 注入）
    ② 与 ③ 是两个渲染器、两种受众，不要混用——详见 render_schema_md 的 docstring。
"""
from __future__ import annotations

TABLES: dict[str, dict] = {
    "customers": {
        "comment": "客户表：下单客户唯一标识与所在州/城市",
        "ddl": """
CREATE TABLE customers (
  customer_id       VARCHAR(32) NOT NULL PRIMARY KEY,
  customer_unique_id VARCHAR(32) NOT NULL,
  customer_zip_code_prefix VARCHAR(10),
  customer_city     VARCHAR(64),
  customer_state    VARCHAR(2)
)""",
        "columns": {
            "customer_id": "客户ID，主键（每笔订单对应一个，关联orders.customer_id）",
            "customer_unique_id": "客户唯一ID（同一客户多次下单此ID相同，用于复购分析）",
            "customer_zip_code_prefix": "客户邮编前缀（前5位）",
            "customer_city": "客户所在城市",
            "customer_state": "客户所在州（两位缩写，如SP圣保罗/RJ里约/MG米纳斯）",
        },
    },
    "orders": {
        "comment": "订单主表：事实表，记录订单状态与各环节时间戳",
        "ddl": """
CREATE TABLE orders (
  order_id                         VARCHAR(32) NOT NULL PRIMARY KEY,
  customer_id                      VARCHAR(32) NOT NULL,
  order_status                     VARCHAR(20) NOT NULL,
  order_purchase_timestamp         DATETIME NOT NULL,
  order_approved_at                DATETIME,
  order_delivered_carrier_date     DATETIME,
  order_delivered_customer_date    DATETIME,
  order_estimated_delivery_date    DATETIME NOT NULL
)""",
        "columns": {
            "order_id": "订单ID，主键（关联order_items/order_payments/order_reviews）",
            "customer_id": "客户ID，关联customers.customer_id",
            "order_status": ("订单状态枚举：delivered已送达(绝大多数) / shipped运输中 / "
                             "invoiced已开票 / processing备货中 / canceled已取消 / "
                             "unavailable缺货 / approved已确认 / created已创建"),
            "order_purchase_timestamp": "下单时间",
            "order_approved_at": "付款审核通过时间（可为空）",
            "order_delivered_carrier_date": "交付物流承运商时间（可为空）",
            "order_delivered_customer_date": "实际送达客户时间（可为空，未送达的订单为NULL）",
            "order_estimated_delivery_date": "承诺送达时间（预计交付日）",
        },
    },
    "order_items": {
        "comment": "订单明细表：订单行项目，连接订单/商品/卖家三方的桥表",
        "ddl": """
CREATE TABLE order_items (
  order_id               VARCHAR(32) NOT NULL,
  order_item_id          INTEGER NOT NULL,
  product_id             VARCHAR(32) NOT NULL,
  seller_id              VARCHAR(32) NOT NULL,
  shipping_limit_date    DATETIME NOT NULL,
  price                  REAL NOT NULL,
  freight_value          REAL NOT NULL,
  PRIMARY KEY (order_id, order_item_id)
)""",
        "columns": {
            "order_id": "订单ID，关联orders.order_id",
            "order_item_id": "行项目序号（同一订单内从1递增，与order_id构成复合主键）",
            "product_id": "商品ID，关联products.product_id",
            "seller_id": "卖家ID，关联sellers.seller_id",
            "shipping_limit_date": "卖家最晚发货期限",
            "price": "商品单价（巴西雷亚尔BRL，未含运费）",
            "freight_value": "该行分摊的运费（BRL）",
        },
    },
    "order_payments": {
        "comment": "订单支付表：一笔订单可拆多次支付（分期/多方式组合）",
        "ddl": """
CREATE TABLE order_payments (
  order_id               VARCHAR(32) NOT NULL,
  payment_sequential     INTEGER NOT NULL,
  payment_type           VARCHAR(20) NOT NULL,
  payment_installments   INTEGER NOT NULL,
  payment_value          REAL,
  PRIMARY KEY (order_id, payment_sequential)
)""",
        "columns": {
            "order_id": "订单ID，关联orders.order_id",
            "payment_sequential": "支付序号（同一订单多笔支付时从1递增）",
            "payment_type": ("支付方式枚举：credit_card信用卡(最常见) / boleto巴西银行付款单 / "
                             "voucher代金券 / debit_card借记卡 / not_defined未定义"),
            "payment_installments": "分期数（1=一次性付清；6=分6期）",
            "payment_value": "该笔支付金额（BRL）；订单总额=同order_id各笔SUM(payment_value)",
        },
    },
    "order_reviews": {
        "comment": "订单评价表：客户满意度1-5分，可含评论标题与正文。"
                   "注意：官方数据存在少量重复 review_id（同一评价挂多订单），故不设主键",
        "ddl": """
CREATE TABLE order_reviews (
  review_id                  VARCHAR(32) NOT NULL,
  order_id                   VARCHAR(32) NOT NULL,
  review_score               INTEGER NOT NULL,
  review_comment_title       VARCHAR(128),
  review_comment_message     TEXT,
  review_creation_date       DATETIME,
  review_answer_timestamp    DATETIME
)""",
        "columns": {
            "review_id": "评价ID（官方数据存在少量重复，同一order_id也可能有多条评价）",
            "order_id": "订单ID，关联orders.order_id",
            "review_score": "评价分数1-5（1最差5最好）",
            "review_comment_title": "评论标题（可为空）",
            "review_comment_message": "评论正文（可为空，葡语原文）",
            "review_creation_date": "评价发起时间",
            "review_answer_timestamp": "评价回复时间",
        },
    },
    "products": {
        "comment": "商品表：品类与物理属性（重量/尺寸）",
        "ddl": """
CREATE TABLE products (
  product_id                 VARCHAR(32) NOT NULL PRIMARY KEY,
  product_category_name      VARCHAR(64),
  product_name_lenght        INTEGER,
  product_description_lenght INTEGER,
  product_photos_qty         INTEGER,
  product_weight_g           INTEGER,
  product_length_cm          INTEGER,
  product_height_cm          INTEGER,
  product_width_cm           INTEGER
)""",
        "columns": {
            "product_id": "商品ID，主键，关联order_items.product_id",
            "product_category_name": "商品品类（葡语，需join product_category_translation译成英文）",
            "product_name_lenght": "商品名字符数",
            "product_description_lenght": "商品描述字符数",
            "product_photos_qty": "商品图片数量",
            "product_weight_g": "商品重量（克）",
            "product_length_cm": "商品长（厘米）",
            "product_height_cm": "商品高（厘米）",
            "product_width_cm": "商品宽（厘米）",
        },
    },
    "sellers": {
        "comment": "卖家表：入驻商家所在邮编/州/城市",
        "ddl": """
CREATE TABLE sellers (
  seller_id                 VARCHAR(32) NOT NULL PRIMARY KEY,
  seller_zip_code_prefix    VARCHAR(10),
  seller_city               VARCHAR(64),
  seller_state              VARCHAR(2)
)""",
        "columns": {
            "seller_id": "卖家ID，主键，关联order_items.seller_id",
            "seller_zip_code_prefix": "卖家邮编前缀",
            "seller_city": "卖家所在城市",
            "seller_state": "卖家所在州（两位缩写）",
        },
    },
    "geolocation": {
        "comment": "地理坐标表：邮编前缀 → 经纬度（一个前缀可对应多坐标，有重复）",
        "ddl": """
CREATE TABLE geolocation (
  geolocation_zip_code_prefix VARCHAR(10) NOT NULL,
  geolocation_lat             REAL,
  geolocation_lng             REAL,
  geolocation_city            VARCHAR(64),
  geolocation_state           VARCHAR(2)
)""",
        "columns": {
            "geolocation_zip_code_prefix": "邮编前缀（约1.9万唯一值，原表100万行含重复坐标）",
            "geolocation_lat": "纬度",
            "geolocation_lng": "经度",
            "geolocation_city": "城市",
            "geolocation_state": "州",
        },
    },
    "product_category_translation": {
        "comment": "品类翻译表：葡语品类名 → 英文",
        "ddl": """
CREATE TABLE product_category_translation (
  product_category_name          VARCHAR(64) NOT NULL PRIMARY KEY,
  product_category_name_english  VARCHAR(64) NOT NULL
)""",
        "columns": {
            "product_category_name": "葡语品类名（关联products.product_category_name）",
            "product_category_name_english": "英文品类名（如cama_mesa_banho→bed_bath_table）",
        },
    },
}

# 外键关系（供 Schema 剪枝与生成 SQL 时的 join 提示）
RELATIONS: list[str] = [
    "orders.customer_id → customers.customer_id",
    "order_items.order_id → orders.order_id",
    "order_items.product_id → products.product_id",
    "order_items.seller_id → sellers.seller_id",
    "order_payments.order_id → orders.order_id",
    "order_reviews.order_id → orders.order_id",
    "products.product_category_name → product_category_translation.product_category_name",
    "geolocation.geolocation_zip_code_prefix ≈ customers/sellers 的 zip_code_prefix",
]


def render_schema_md(row_counts: dict[str, int] | None = None) -> str:
    """把 TABLES 渲染成**给人阅读**的 Markdown 文档（产物 data/schema.md，随仓库提交）。

    与 prompt 路径的分工（别混用）：
      - 本函数 → 人类读者：Markdown 表格 + 代码块，方便 review 表结构与字段含义；
      - agent.prompting.render_schema_block → LLM：DDL + 行内 -- 注释 + 采样行，
        更省 token 且列名与解释零距离对齐。
    同一份 TABLES，两种受众，两种渲染形态。

    调用点：scripts/init_db.py --dump-schema（建库后自动重新生成，保证文档与库同步）。
    row_counts 可选：给定时在表名后标注行数，缺某表则跳过该标注（动态表可能不在库中）。
    """
    lines = ["# Olist 数据库 Schema（中文注释）", ""]
    for name, t in TABLES.items():
        cnt = f"（{row_counts[name]:,} 行）" if row_counts and name in row_counts else ""
        lines += [f"## {name} {cnt}", f"> {t['comment']}", "", "```sql", t["ddl"].strip(), "```",
                  "", "| 字段 | 说明 |", "|---|---|"]
        lines += [f"| {c} | {cm} |" for c, cm in t["columns"].items()]
        lines.append("")
    lines += ["## 外键关系", ""] + [f"- {r}" for r in RELATIONS]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 动态表注册（P2-S9 CSV 导入）：新表进白名单 + 进 Schema 注入，Agent 可查
# ---------------------------------------------------------------------------


def _settings_db_path() -> str:
    from src.core.config import settings
    return str(settings.db_path)

def register_dynamic_table(name: str, comment: str, ddl: str, columns: dict[str, str]) -> None:
    """把 CSV 导入产生的表注册进 TABLES 与白名单（原地 mutate，保证所有
    `from ... import ALLOWED_TABLES` 的既有引用同步可见——重绑定会让别名拿到旧对象）。"""
    if name in TABLES:
        return
    TABLES[name] = {"comment": comment, "ddl": ddl, "columns": columns}
    # validator.ALLOWED_TABLES 与本模块共享同一 set 对象（模块顶层 set(TABLES.keys())
    # 是当时的浅拷贝，动态表必须显式补进去）
    from src.safety import validator
    validator.ALLOWED_TABLES.add(name)


def load_custom_tables() -> int:
    """服务启动时恢复历史导入的 CSV 表（注册信息落盘 data/db/custom_tables.json）。"""
    import json
    from pathlib import Path
    reg = Path(_settings_db_path()).parent / "custom_tables.json"
    if not reg.exists():
        return 0
    try:
        metas = json.loads(reg.read_text(encoding="utf-8"))
    except Exception:
        return 0
    for m in metas:
        register_dynamic_table(m["name"], m["comment"], m["ddl"], m["columns"])
    return len(metas)


def save_custom_table_meta(name: str, comment: str, ddl: str, columns: dict[str, str],
                           db_path: str = "") -> None:
    """注册信息落盘（重启后 load_custom_tables 恢复；表本体已在 SQLite 里持久）。
    db_path 显式传参：注册文件跟随目标库所在目录，测试用临时库时不写穿真实库。"""
    import json
    from pathlib import Path
    reg = (Path(db_path).parent if db_path
           else Path(_settings_db_path())) / "custom_tables.json"
    metas = []
    if reg.exists():
        try:
            metas = json.loads(reg.read_text(encoding="utf-8"))
        except Exception:
            metas = []
    metas = [m for m in metas if m["name"] != name]
    metas.append({"name": name, "comment": comment, "ddl": ddl, "columns": columns})
    reg.write_text(json.dumps(metas, ensure_ascii=False, indent=1), encoding="utf-8")

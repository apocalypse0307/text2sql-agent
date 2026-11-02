"""Olist 巴西电商场景包（当前唯一内置场景，全引擎默认值）。

数值来源：Olist 公开数据集 9 表入库后的真实范围（见 scripts/init_db.py）。
改动纪律：这里改一个字段 = 全部 prompt 的对应口径同步变化；金标（evals/goldset*.json）
的口径必须与本文件一致，否则评测失去区分度（S5 踩过的坑）。
"""
from src.scenarios.base import ScenarioProfile

OLIST = ScenarioProfile(
    name="olist",
    domain="电商",
    dialect="SQLite",
    known_max_year=2018,
    data_range="2016-09 ~ 2018-10",
    table_catalog=("customers/orders/order_items/order_payments/order_reviews/"
                   "products/sellers/geolocation/product_category_translation"),
    default_status="delivered",
    dedup_key="customer_unique_id",
    currency="BRL",
    db_desc="Olist 电商数据",
)

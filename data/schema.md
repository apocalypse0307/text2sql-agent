# Olist 数据库 Schema（中文注释）

## customers （99,441 行）
> 客户表：下单客户唯一标识与所在州/城市

```sql
CREATE TABLE customers (
  customer_id       VARCHAR(32) NOT NULL PRIMARY KEY,
  customer_unique_id VARCHAR(32) NOT NULL,
  customer_zip_code_prefix VARCHAR(10),
  customer_city     VARCHAR(64),
  customer_state    VARCHAR(2)
)
```

| 字段 | 说明 |
|---|---|
| customer_id | 客户ID，主键（每笔订单对应一个，关联orders.customer_id） |
| customer_unique_id | 客户唯一ID（同一客户多次下单此ID相同，用于复购分析） |
| customer_zip_code_prefix | 客户邮编前缀（前5位） |
| customer_city | 客户所在城市 |
| customer_state | 客户所在州（两位缩写，如SP圣保罗/RJ里约/MG米纳斯） |

## orders （99,441 行）
> 订单主表：事实表，记录订单状态与各环节时间戳

```sql
CREATE TABLE orders (
  order_id                         VARCHAR(32) NOT NULL PRIMARY KEY,
  customer_id                      VARCHAR(32) NOT NULL,
  order_status                     VARCHAR(20) NOT NULL,
  order_purchase_timestamp         DATETIME NOT NULL,
  order_approved_at                DATETIME,
  order_delivered_carrier_date     DATETIME,
  order_delivered_customer_date    DATETIME,
  order_estimated_delivery_date    DATETIME NOT NULL
)
```

| 字段 | 说明 |
|---|---|
| order_id | 订单ID，主键（关联order_items/order_payments/order_reviews） |
| customer_id | 客户ID，关联customers.customer_id |
| order_status | 订单状态枚举：delivered已送达(绝大多数) / shipped运输中 / invoiced已开票 / processing备货中 / canceled已取消 / unavailable缺货 / approved已确认 / created已创建 |
| order_purchase_timestamp | 下单时间 |
| order_approved_at | 付款审核通过时间（可为空） |
| order_delivered_carrier_date | 交付物流承运商时间（可为空） |
| order_delivered_customer_date | 实际送达客户时间（可为空，未送达的订单为NULL） |
| order_estimated_delivery_date | 承诺送达时间（预计交付日） |

## order_items （112,650 行）
> 订单明细表：订单行项目，连接订单/商品/卖家三方的桥表

```sql
CREATE TABLE order_items (
  order_id               VARCHAR(32) NOT NULL,
  order_item_id          INTEGER NOT NULL,
  product_id             VARCHAR(32) NOT NULL,
  seller_id              VARCHAR(32) NOT NULL,
  shipping_limit_date    DATETIME NOT NULL,
  price                  REAL NOT NULL,
  freight_value          REAL NOT NULL,
  PRIMARY KEY (order_id, order_item_id)
)
```

| 字段 | 说明 |
|---|---|
| order_id | 订单ID，关联orders.order_id |
| order_item_id | 行项目序号（同一订单内从1递增，与order_id构成复合主键） |
| product_id | 商品ID，关联products.product_id |
| seller_id | 卖家ID，关联sellers.seller_id |
| shipping_limit_date | 卖家最晚发货期限 |
| price | 商品单价（巴西雷亚尔BRL，未含运费） |
| freight_value | 该行分摊的运费（BRL） |

## order_payments （103,886 行）
> 订单支付表：一笔订单可拆多次支付（分期/多方式组合）

```sql
CREATE TABLE order_payments (
  order_id               VARCHAR(32) NOT NULL,
  payment_sequential     INTEGER NOT NULL,
  payment_type           VARCHAR(20) NOT NULL,
  payment_installments   INTEGER NOT NULL,
  payment_value          REAL,
  PRIMARY KEY (order_id, payment_sequential)
)
```

| 字段 | 说明 |
|---|---|
| order_id | 订单ID，关联orders.order_id |
| payment_sequential | 支付序号（同一订单多笔支付时从1递增） |
| payment_type | 支付方式枚举：credit_card信用卡(最常见) / boleto巴西银行付款单 / voucher代金券 / debit_card借记卡 / not_defined未定义 |
| payment_installments | 分期数（1=一次性付清；6=分6期） |
| payment_value | 该笔支付金额（BRL）；订单总额=同order_id各笔SUM(payment_value) |

## order_reviews （99,224 行）
> 订单评价表：客户满意度1-5分，可含评论标题与正文。注意：官方数据存在少量重复 review_id（同一评价挂多订单），故不设主键

```sql
CREATE TABLE order_reviews (
  review_id                  VARCHAR(32) NOT NULL,
  order_id                   VARCHAR(32) NOT NULL,
  review_score               INTEGER NOT NULL,
  review_comment_title       VARCHAR(128),
  review_comment_message     TEXT,
  review_creation_date       DATETIME,
  review_answer_timestamp    DATETIME
)
```

| 字段 | 说明 |
|---|---|
| review_id | 评价ID（官方数据存在少量重复，同一order_id也可能有多条评价） |
| order_id | 订单ID，关联orders.order_id |
| review_score | 评价分数1-5（1最差5最好） |
| review_comment_title | 评论标题（可为空） |
| review_comment_message | 评论正文（可为空，葡语原文） |
| review_creation_date | 评价发起时间 |
| review_answer_timestamp | 评价回复时间 |

## products （32,951 行）
> 商品表：品类与物理属性（重量/尺寸）

```sql
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
)
```

| 字段 | 说明 |
|---|---|
| product_id | 商品ID，主键，关联order_items.product_id |
| product_category_name | 商品品类（葡语，需join product_category_translation译成英文） |
| product_name_lenght | 商品名字符数 |
| product_description_lenght | 商品描述字符数 |
| product_photos_qty | 商品图片数量 |
| product_weight_g | 商品重量（克） |
| product_length_cm | 商品长（厘米） |
| product_height_cm | 商品高（厘米） |
| product_width_cm | 商品宽（厘米） |

## sellers （3,095 行）
> 卖家表：入驻商家所在邮编/州/城市

```sql
CREATE TABLE sellers (
  seller_id                 VARCHAR(32) NOT NULL PRIMARY KEY,
  seller_zip_code_prefix    VARCHAR(10),
  seller_city               VARCHAR(64),
  seller_state              VARCHAR(2)
)
```

| 字段 | 说明 |
|---|---|
| seller_id | 卖家ID，主键，关联order_items.seller_id |
| seller_zip_code_prefix | 卖家邮编前缀 |
| seller_city | 卖家所在城市 |
| seller_state | 卖家所在州（两位缩写） |

## geolocation （1,000,163 行）
> 地理坐标表：邮编前缀 → 经纬度（一个前缀可对应多坐标，有重复）

```sql
CREATE TABLE geolocation (
  geolocation_zip_code_prefix VARCHAR(10) NOT NULL,
  geolocation_lat             REAL,
  geolocation_lng             REAL,
  geolocation_city            VARCHAR(64),
  geolocation_state           VARCHAR(2)
)
```

| 字段 | 说明 |
|---|---|
| geolocation_zip_code_prefix | 邮编前缀（约1.9万唯一值，原表100万行含重复坐标） |
| geolocation_lat | 纬度 |
| geolocation_lng | 经度 |
| geolocation_city | 城市 |
| geolocation_state | 州 |

## product_category_translation （71 行）
> 品类翻译表：葡语品类名 → 英文

```sql
CREATE TABLE product_category_translation (
  product_category_name          VARCHAR(64) NOT NULL PRIMARY KEY,
  product_category_name_english  VARCHAR(64) NOT NULL
)
```

| 字段 | 说明 |
|---|---|
| product_category_name | 葡语品类名（关联products.product_category_name） |
| product_category_name_english | 英文品类名（如cama_mesa_banho→bed_bath_table） |

## 外键关系

- orders.customer_id → customers.customer_id
- order_items.order_id → orders.order_id
- order_items.product_id → products.product_id
- order_items.seller_id → sellers.seller_id
- order_payments.order_id → orders.order_id
- order_reviews.order_id → orders.order_id
- products.product_category_name → product_category_translation.product_category_name
- geolocation.geolocation_zip_code_prefix ≈ customers/sellers 的 zip_code_prefix

# 数据说明书

> 这份说明书会放进 Agent 的 System Prompt。写 SQL 之前，先按这里的口径和注意事项理解数据。
> 具体数字（行数、缺失比例、参考指标）见同目录下自动生成的 `data_profile.md`。

## 1. 数据是什么

- 来源：Kaggle 数据集 *eCommerce behavior data from multi category store*（REES46 平台），一个多品类电商网站 **2019 年 10 月**的用户行为日志。
- 一条记录 = 一个用户在某个时间对某个商品做了一次操作：浏览（view）、加购（cart）或购买（purchase）。
- **没有订单表**：没有订单号，也没有购买数量。
- **这是抽样数据**：全量共 42,448,764 条事件、3,022,290 名用户；按 `user_id % 10 = 0` 抽样后剩 4,224,899 条事件、301,732 名用户，约占 10%。
  - 被抽中的用户，其行为是**完整的**，所以比率类指标（转化率、平均成交价、人均事件数）可以代表全站。
  - 绝对数（用户数、事件数、GMV）只有全站的约 1/10。回答时必须说明「以下为样本数据」；不要自行乘以 10 当作全站数字，除非用户明确要求估算，并且要注明是估算。

## 2. 四张表及关联方式

| 表 | 一行代表 | 主键 | 用途 |
| --- | --- | --- | --- |
| `events` | 一个行为事件（事实表） | `event_id` | 几乎所有分析都从这里开始 |
| `products` | 一个商品 | `product_id` | 品牌、所属品类 |
| `categories` | 一个品类 | `category_id` | 品类名称、一级/二级品类 |
| `sessions` | 一次会话 | `user_session` | 会话级漏斗、会话时长 |

关联方式：

```
events.product_id   = products.product_id
products.category_id = categories.category_id
events.user_session  = sessions.user_session
events.user_id       = sessions.user_id   （同一用户）
```

- 价格只在 `events.price` 里，是**事件发生时**的价格。同一商品在月内会变价（全量数据中约 34.5% 的商品价格变过），不存在「商品的固定价格」。
- `sessions` 是从 `events` 汇总出来的，`user_session` 为空的事件不在里面。

## 3. 指标口径（没有特别说明时，一律按这里算）

| 指标 | 定义 | SQL 要点 |
| --- | --- | --- |
| 有效购买 | `event_type = 'purchase'` 且 `price > 0` 的事件 | 一条有效购买约定为买了 1 件 |
| GMV | 有效购买的 `price` 之和 | `SUM(price) ... WHERE event_type='purchase' AND price > 0` |
| 购买次数 | 有效购买的事件数 | `COUNT(*)` |
| 购买用户数 | 有过有效购买的用户数 | `COUNT(DISTINCT user_id)`，不能用 `COUNT(*)` |
| 平均成交价 | GMV ÷ 购买次数 | 没有订单，所以不叫「客单价」 |
| 人均消费 | GMV ÷ 购买用户数 | |
| 复购用户 | 在 ≥ 2 个**不同会话**中有有效购买的用户 | 同一会话里买多件只算一次购物；只覆盖 10 月 |
| 复购率 | 复购用户数 ÷ 购买用户数 | |
| 品类 | 默认用一级品类 `categories.level1` | 需要更细时用 `level2` 或 `category_code` |
| 时间 | `event_time` 是 **UTC** | 按天分析用 `DATE(event_time)` |

### 转化率有三种口径，结果差别很大

| 口径 | 计算方式 | 适合回答 |
| --- | --- | --- |
| 用户级（默认） | 有购买的用户数 ÷ 有浏览的用户数 | 「多少比例的访客最终下单」 |
| 会话级 | 有购买的会话数 ÷ 有浏览的会话数（用 `sessions` 表） | 「单次访问的转化效率」 |
| 事件级 | 购买事件数 ÷ 浏览事件数 | 「每次浏览带来多少购买」 |

问题没有指明口径时，用**用户级**，并在回答里写明口径。

## 4. 数据坑（写 SQL 前逐条检查）

1. **金额类指标要排除 `price <= 0`**：数据里有 0 元的异常记录（例如同一商品一直标价 300 多，中间出现一条 0 元）。
2. **用户数必须去重**：`COUNT(DISTINCT user_id)`。
3. **购买不一定经过加购**：大量购买之前没有加购事件，所以「加购→购买转化率」不能简单用购买数 ÷ 加购数，要么按用户/会话判断「先加购再购买」，要么说明局限。
4. **品牌缺失**：约 26% 的商品没有品牌（全量数据中 166,794 个商品里有 43,613 个）。按品牌排名时，`brand IS NULL` 的部分要单独说明，不能当成一个品牌参与排名。品牌名是小写。
5. **品类缺失**：配件等商品常常没有 `category_code`，此时 `level1` / `level2` 也为空。按品类统计时，把空值归为「未分类」并说明占比。
6. **不同 `category_id` 可能对应相同的 `category_code`**：按品类名统计时，按 `level1` / `level2` / `category_code` 分组，不要按 `category_id` 分组后当作不同品类。
7. **少量 `user_session` 为空**：会话级分析会漏掉这些事件，用户级分析不受影响。
8. **重复行已删除**：导入时删掉了所有字段完全相同的重复记录，不需要再去重。
9. **时间边界**：数据只有 10 月一个月，无法分析月度趋势、长期留存或季节性。按小时分析时注意是 UTC 时间，用户不一定在 UTC 时区。
10. **相关不等于因果**：这是观察数据。比较两组（例如不同价格段的转化率）时，只能说明相关，应提示可能的混杂因素（品类、用户意图等）。

## 5. 写 SQL 的注意事项（MySQL 8）

- `events` 有几百万行：不要 `SELECT *` 不加条件；探查数据时加 `LIMIT`。
- 已有索引：`events(user_id)`、`events(event_type, event_time)`、`events(product_id)`、`events(user_session)`、`sessions(user_id)`、`categories(level1)`。按事件类型和时间过滤的查询最快。
- 可以用 `WITH`（CTE）和窗口函数（`ROW_NUMBER()`、`LAG()` 等），复杂问题先拆成多步。
- MySQL 不支持 `FULL OUTER JOIN`。
- 日期函数：`DATE(event_time)`、`HOUR(event_time)`、`DATE_FORMAT(event_time, '%Y-%m-%d')`、`DAYOFWEEK(event_time)`。
- 比率用 `x / NULLIF(y, 0)`，避免除以 0；百分比用 `ROUND(..., 4)` 保留精度。

-- =====================================================================
-- ecom 数据库建表脚本（REES46 2019-10 按用户抽样，4 张表，星型模型）
-- 运行方式：由 scripts/load_data.py 自动执行（也可以手动：mysql -u root -p ecom < sql/schema.sql）
-- 说明：
--   * 不建外键：导入更快，表之间的关系由字段注释和 schema_notes.md 约定
--   * 索引在导入数据之后再建（见 sql/indexes.sql），否则导入会很慢
--   * 字段 COMMENT 会被 describe_table 工具读出来给 Agent 看，数据坑写在这里
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. events：事实表，一行 = 一个用户行为事件
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS events;

CREATE TABLE events (
    event_id     BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键，原始数据没有；不代表订单号',
    event_time   DATETIME      NOT NULL COMMENT '事件时间，UTC 时区',
    event_type   VARCHAR(20)   NOT NULL COMMENT '取值 view / cart / purchase；一条 purchase 约定为购买 1 件',
    product_id   BIGINT        NOT NULL COMMENT '关联 products.product_id',
    price        DECIMAL(10,2) NOT NULL COMMENT '事件发生时的价格（同一商品会变价）；存在 price<=0 的异常值，算 GMV 时排除',
    user_id      BIGINT        NOT NULL COMMENT '用户永久 ID；算用户数用 COUNT(DISTINCT user_id)',
    user_session VARCHAR(64)   NULL     COMMENT '会话 ID，关联 sessions.user_session；少量为空'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='用户行为事件（事实表）。抽样数据：约为全量用户的 1/10，绝对数不能当全量报告';


-- ---------------------------------------------------------------------
-- 2. products：商品维度表，一行 = 一个商品（不含价格，价格在 events 里）
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS products;

CREATE TABLE products (
    product_id  BIGINT       NOT NULL COMMENT '商品 ID，主键',
    category_id BIGINT       NOT NULL COMMENT '关联 categories.category_id；已验证每个商品只属于一个品类',
    brand       VARCHAR(100) NULL     COMMENT '品牌（小写）。取该商品出现次数最多的非空值；约 26% 的商品为空',
    PRIMARY KEY (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='商品维度表。按品牌统计时需说明品牌缺失的部分如何处理';


-- ---------------------------------------------------------------------
-- 3. categories：品类维度表，一行 = 一个品类
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS categories;

CREATE TABLE categories (
    category_id   BIGINT       NOT NULL COMMENT '品类 ID，主键',
    category_code VARCHAR(255) NULL     COMMENT '完整品类路径，如 electronics.smartphone；配件类等常为空',
    level1        VARCHAR(100) NULL     COMMENT '一级品类，category_code 按点号拆分的第 1 段，如 electronics',
    level2        VARCHAR(100) NULL     COMMENT '二级品类，第 2 段，如 smartphone；部分品类只有一级时为空',
    PRIMARY KEY (category_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='品类维度表。不同 category_id 可能对应相同的 category_code，按品类名统计时用 level1/level2 或 category_code 分组';


-- ---------------------------------------------------------------------
-- 4. sessions：会话汇总表（由 events 派生），一行 = 一次会话
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS sessions;

CREATE TABLE sessions (
    user_session VARCHAR(64) NOT NULL COMMENT '会话 ID，主键；user_session 为空的事件不进入本表',
    user_id      BIGINT      NOT NULL COMMENT '会话所属用户',
    start_time   DATETIME    NOT NULL COMMENT '会话内第一个事件的时间，UTC',
    end_time     DATETIME    NOT NULL COMMENT '会话内最后一个事件的时间，UTC',
    n_views      INT         NOT NULL DEFAULT 0 COMMENT '会话内 view 事件数',
    n_carts      INT         NOT NULL DEFAULT 0 COMMENT '会话内 cart 事件数',
    n_purchases  INT         NOT NULL DEFAULT 0 COMMENT '会话内 purchase 事件数',
    PRIMARY KEY (user_session)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='会话汇总表，用于会话级漏斗。注意：会话级转化率与用户级、事件级口径不同';


-- 索引不在这里建：导入完数据之后由 scripts/load_data.py 执行 sql/indexes.sql

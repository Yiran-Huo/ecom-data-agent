-- 导入数据之后再建索引（先建索引再导入，每插入一行都要更新索引，会慢很多）
-- 由 scripts/load_data.py 自动执行

-- 按用户查行为：漏斗、复购、用户分群
CREATE INDEX idx_events_user      ON events(user_id);
-- 按事件类型 + 时间过滤：最常见的查询，例如「10 月每天的购买数」
CREATE INDEX idx_events_type_time ON events(event_type, event_time);
-- 按商品 join products：品牌、品类分析
CREATE INDEX idx_events_product   ON events(product_id);
-- 按会话 join sessions
CREATE INDEX idx_events_session   ON events(user_session);
-- 按用户查会话
CREATE INDEX idx_sessions_user    ON sessions(user_id);
-- 按一级品类分组
CREATE INDEX idx_categories_l1    ON categories(level1);

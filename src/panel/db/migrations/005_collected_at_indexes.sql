-- 005_collected_at_indexes.sql — MS-005 评审修复 (async-perf #7/#8)
--
-- 两张主时序表的 retention / 降采样路径都按 collected_at 做时间范围过滤,但既有
-- 索引的前导列都不是 collected_at,因此 EXPLAIN QUERY PLAN 显示全表 SCAN:
--
--   gpu_metrics       idx_gpu_metrics_query 前导列 server_id;5min 降采样 job 每 5
--                     分钟对该表做聚合 + 48h 边界删除两次全表扫描。
--   metric_history    idx_history_query 前导列 collector,collected_at 在第 4 位,
--                     无法服务仅含 collected_at 的范围条件;每日 retention DELETE
--                     全表 SCAN。
--
-- 补两条以 collected_at 为前导列的独立索引,使范围过滤变 range seek。两表 DDL 全
-- 部 IF NOT EXISTS,migrate.run() 幂等,可重复执行。fresh install 见 schema.sql。

-- gpu_metrics: 让 aggregate_raw_buckets / delete_raw_metrics_before 走区间 seek
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_collected
    ON gpu_metrics (collected_at);

-- metric_history: 让 prune_history 的 DELETE ... WHERE collected_at < ? 走区间 seek
CREATE INDEX IF NOT EXISTS idx_history_collected
    ON metric_history (collected_at);

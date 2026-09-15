-- 압축 정책: 7일 지난 chunk 를 컬럼 압축. segmentby=symbol, orderby=시각.
-- TimescaleDB Apache 전용 빌드에서는 실패하므로 db.apply_schema 가 실패를 기록만 하고 넘어간다.
DO $$
BEGIN
    IF NOT (SELECT compression_enabled FROM timescaledb_information.hypertables
            WHERE hypertable_schema = 'market' AND hypertable_name = 'trades') THEN
        ALTER TABLE market.trades SET (timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby = 'exchange_ts, agg_id');
    END IF;
    IF NOT (SELECT compression_enabled FROM timescaledb_information.hypertables
            WHERE hypertable_schema = 'market' AND hypertable_name = 'book_top5') THEN
        ALTER TABLE market.book_top5 SET (timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby = 'exchange_ts, update_id');
    END IF;
END $$;
SELECT add_compression_policy('market.trades', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('market.book_top5', INTERVAL '7 days', if_not_exists => TRUE);

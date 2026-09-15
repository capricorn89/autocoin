-- autocoin 시장데이터 스키마 (PostgreSQL + TimescaleDB). 여러 번 실행해도 안전(멱등).
--
-- 파티셔닝 기준: 시장데이터 hypertable 은 거래소 시각(exchange_ts) 기준 1일 chunk.
--   - EWYUSDT 하루 체결 약 39만 + 5호가 변경 최대 약 81만 행 → chunk 당 수백 MB, 메모리에 올릴 수 있는 크기
--   - 분석·정합성 검사 단위(일)와 일치, 압축도 일 단위로 적용됨
--   - 수신 시각(recv_ts)이 아니라 거래소 시각으로 나눠야 REST 백필 행이 올바른 chunk 로 들어감
-- 백업: 하지 않음 (사용자 결정 2026-09-15).
-- 가격/수량은 double precision. tick 0.01 종목에서 비교는 허용오차로 할 것.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA IF NOT EXISTS market;

-- 체결 (Binance aggTrade)
CREATE TABLE IF NOT EXISTS market.trades (
    exchange_ts     timestamptz      NOT NULL,  -- T: 체결 시각
    symbol          text             NOT NULL,
    agg_id          bigint           NOT NULL,  -- a: 집계 체결 id (연속이어야 함)
    price           double precision NOT NULL,  -- p
    qty             double precision NOT NULL,  -- q
    first_trade_id  bigint           NOT NULL,  -- f
    last_trade_id   bigint           NOT NULL,  -- l
    is_buyer_maker  boolean          NOT NULL,  -- m: true = 매수자가 maker = 매도 주도 체결
    event_ts        timestamptz,                -- E (REST 백필 행은 NULL)
    recv_ts         timestamptz,                -- 로컬 수신 시각 (REST 백필 행은 NULL)
    source          text             NOT NULL DEFAULT 'ws',   -- ws | rest_backfill
    PRIMARY KEY (symbol, exchange_ts, agg_id)
);
SELECT create_hypertable('market.trades', by_range('exchange_ts', INTERVAL '1 day'),
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS trades_symbol_agg_id_idx ON market.trades (symbol, agg_id);

-- 매수/매도 5호가: 상위 5단계 가격·수량이 바뀐 시점마다 1행 (변화 없는 diff 는 저장하지 않음)
CREATE TABLE IF NOT EXISTS market.book_top5 (
    exchange_ts  timestamptz NOT NULL,  -- depthUpdate T: 거래소 트랜잭션 시각
    symbol       text        NOT NULL,
    update_id    bigint      NOT NULL,  -- u: 이 상태를 만든 마지막 diff 의 update id
    event_ts     timestamptz NOT NULL,  -- E
    recv_ts      timestamptz NOT NULL,
    bid_px_1 double precision, bid_px_2 double precision, bid_px_3 double precision,
    bid_px_4 double precision, bid_px_5 double precision,
    bid_qty_1 double precision, bid_qty_2 double precision, bid_qty_3 double precision,
    bid_qty_4 double precision, bid_qty_5 double precision,
    ask_px_1 double precision, ask_px_2 double precision, ask_px_3 double precision,
    ask_px_4 double precision, ask_px_5 double precision,
    ask_qty_1 double precision, ask_qty_2 double precision, ask_qty_3 double precision,
    ask_qty_4 double precision, ask_qty_5 double precision,
    PRIMARY KEY (symbol, exchange_ts, update_id)
);
SELECT create_hypertable('market.book_top5', by_range('exchange_ts', INTERVAL '1 day'),
                         if_not_exists => TRUE);

-- 수집기 이벤트: 시퀀스 갭, 연결/끊김, 스냅샷 재동기화, 시계 오프셋 (정합성 검사 근거)
CREATE TABLE IF NOT EXISTS market.collector_events (
    recv_ts  timestamptz NOT NULL,
    kind     text        NOT NULL,   -- gap | conn | snapshot | clock | sink_error
    symbol   text,
    stream   text,
    detail   jsonb       NOT NULL DEFAULT '{}'::jsonb
);
SELECT create_hypertable('market.collector_events', by_range('recv_ts', INTERVAL '30 days'),
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS collector_events_kind_idx ON market.collector_events (kind, recv_ts DESC);

-- 실행(체결) 기록 스키마 (WOO-99). market 과 분리. 여러 번 실행해도 안전(멱등).
--
-- 원본은 스케줄러가 쓰는 data/overnight_live/<mode>/events.jsonl 이고, 여기는 그 적재본 + 파생 지표다.
-- 적재는 (mode, line_no) 로 멱등이라 전체를 다시 넣어도 중복이 생기지 않는다.
-- 행 수가 작아(밤당 수십 행) 일반 테이블로 둔다 (hypertable 아님).

CREATE SCHEMA IF NOT EXISTS exec;

CREATE TABLE IF NOT EXISTS exec.events (
    mode       text        NOT NULL,          -- paper | live
    line_no    integer     NOT NULL,          -- events.jsonl 줄 번호 (0부터)
    logged_at  timestamptz NOT NULL,
    event      text        NOT NULL,          -- start | decision | placed | filled | canceled | ... | night | alert
    client_id  text,
    entry_date date,
    detail     jsonb       NOT NULL,          -- 원본 줄 전체
    PRIMARY KEY (mode, line_no)
);
CREATE INDEX IF NOT EXISTS exec_events_kind_idx ON exec.events (mode, event, entry_date);

CREATE TABLE IF NOT EXISTS exec.nights (
    mode         text    NOT NULL,
    entry_date   date    NOT NULL,
    exit_date    date,
    method       text,
    phase        text    NOT NULL,             -- done | skipped
    spec_version integer,
    qty          double precision,
    entry_price  double precision,
    exit_price   double precision,
    stop_price   double precision,
    fees         double precision,
    funding      double precision,
    pnl          double precision,             -- USDT, 수수료·펀딩 포함
    started_at   timestamptz,
    ended_at     timestamptz,
    notes        jsonb,
    PRIMARY KEY (mode, entry_date)
);

CREATE TABLE IF NOT EXISTS exec.fills (
    mode       text             NOT NULL,
    client_id  text             NOT NULL,
    ts         timestamptz      NOT NULL,     -- 체결 시각 (페이퍼: 시뮬 시각, 라이브: 거래소 시각)
    entry_date date,
    leg        text,                          -- e 진입 | x 청산 | s 손절 | f 강제청산 | r 잔존청산
    side       text             NOT NULL,
    qty        double precision NOT NULL,
    price      double precision NOT NULL,
    fee        double precision NOT NULL,
    liquidity  text             NOT NULL,     -- MAKER | TAKER
    line_no    integer          NOT NULL,
    PRIMARY KEY (mode, line_no)
);

CREATE TABLE IF NOT EXISTS exec.decisions (
    mode         text        NOT NULL,
    entry_date   date        NOT NULL,
    leg          text        NOT NULL,        -- e | x
    method       text,
    scheduled_at timestamptz NOT NULL,        -- 스펙상 구간 시작 시각
    decided_at   timestamptz,                 -- 실제로 호가를 본 시각
    bid          double precision,
    ask          double precision,
    book_ts      timestamptz,
    PRIMARY KEY (mode, entry_date, leg)
);

-- 밤별 판정 지표 (report.py 가 계산해서 덮어쓴다). bp 는 모두 "비용이면 +".
CREATE TABLE IF NOT EXISTS exec.night_metrics (
    mode              text NOT NULL,
    entry_date        date NOT NULL,
    method            text,
    how               text,                   -- 청산 | 손절 | ...
    ref_entry         double precision,       -- 진입 구간 시작 1분봉 시가 (A/B 모두 15:30)
    ref_exit          double precision,       -- 청산 구간 시작 1분봉 시가 (A 08:59, B 08:57)
    bt_entry          double precision,       -- 백테스트 정의 진입 (마감 시각 1분봉 시가)
    bt_exit           double precision,       -- 백테스트 정의 청산 (개장 시각 1분봉 시가)
    slip_entry_bp     double precision,
    slip_exit_bp      double precision,
    slip_entry_mid_bp double precision,       -- 결정 순간 중간가 대비
    slip_exit_mid_bp  double precision,
    fee_bp            double precision,       -- 왕복 수수료 / 진입 명목
    cost_rt_bp        double precision,       -- slip_entry + slip_exit + fee
    maker_entry       double precision,       -- 진입 수량 중 maker 비율
    maker_exit        double precision,
    gross_bp          double precision,       -- 실제 (청산 VWAP / 진입 VWAP - 1), 비용 전
    bt_gross_bp       double precision,       -- 같은 밤 백테스트 재현 (개장 시가 / 마감 시가 - 1)
    tracking_bp       double precision,       -- gross - bt_gross
    on_time           boolean,
    pnl               double precision,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (mode, entry_date)
);

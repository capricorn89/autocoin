-- pastofi 코인판 실행 기록 (WOO-99 exec 스키마 재사용, 페이퍼·라이브는 mode 열로 구분). 여러 번 실행해도 안전(멱등).
--
-- 원본은 실행 프로세스가 쓰는 data/pastofi/<mode>/{trades,signals}.jsonl 이고, 여기는 그 적재본이다.
-- 적재는 (mode, 키) upsert 라 전체를 다시 넣어도 중복이 생기지 않는다.

CREATE SCHEMA IF NOT EXISTS exec;

-- 실제(페이퍼 시뮬 포함) 왕복 거래 한 건 = 한 행
CREATE TABLE IF NOT EXISTS exec.pastofi_trades (
    mode          text             NOT NULL,      -- paper | live
    trade_id      text             NOT NULL,      -- <종목>-<신호 epoch 초>
    symbol        text             NOT NULL,
    day           date             NOT NULL,      -- 신호 UTC 날짜
    t_sig         timestamptz      NOT NULL,      -- 엔진 판단 초 T
    entry_ts      timestamptz,
    exit_ts       timestamptz,
    side          smallint         NOT NULL,      -- -1 숏
    qty           double precision NOT NULL,
    entry_px      double precision,               -- 체결 평균가
    exit_px       double precision,
    assumed_in    double precision,               -- 백테스트 가정가 (신호 시점 bid1)
    assumed_out   double precision,               -- 백테스트 가정가 (청산 판단 시점 ask1). 엔진 밖 청산이면 NULL
    reason        text,                           -- 익절 | 손절 | 5분 | 비상손절 | 5분(재시작) ...
    gross_usd     double precision,               -- 수수료 전
    fees_usd      double precision,
    net_usd       double precision,               -- 수수료 후
    pnl_bp        double precision,               -- 체결가 기준 ln(진입/청산) × 1e4 (숏)
    assumed_bp    double precision,               -- 가정가 기준
    slip_in_bp    double precision,               -- (가정 - 체결) / 가정 × 1e4, 매도라 양수 = 불리
    slip_out_bp   double precision,               -- (체결 - 가정) / 가정 × 1e4, 매수라 양수 = 불리
    past_bp       double precision,
    ofi_ratio     double precision,
    entry_cid     text,
    exit_cid      text,
    spec_version  text,
    detail        jsonb            NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (mode, trade_id)
);
CREATE INDEX IF NOT EXISTS pastofi_trades_day_idx ON exec.pastofi_trades (mode, day);

-- 엔진 로직1 판단 전부 (실제로 들어갔든 건너뛰었든). 건너뛴 신호도 엔진이 계속 돌아 가상 결과가 남는다
CREATE TABLE IF NOT EXISTS exec.pastofi_signals (
    mode          text             NOT NULL,
    symbol        text             NOT NULL,
    t_sig         timestamptz      NOT NULL,
    day           date             NOT NULL,
    status        text             NOT NULL,      -- traded | skipped | open
    skip_reason   text,
    p_in          double precision,               -- 엔진 가정 진입가
    t_out         timestamptz,
    p_out         double precision,               -- 엔진 가정 청산가
    reason        text,                           -- 엔진 청산 사유
    engine_bp     double precision,               -- 엔진 pnl_bp (비용 전)
    past_bp       double precision,
    ofi_ratio     double precision,
    detail        jsonb            NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (mode, symbol, t_sig)
);

-- 매일 일치 대조 (DB 재생 vs 실행 프로세스 엔진 판단)
CREATE TABLE IF NOT EXISTS exec.pastofi_reconcile (
    mode          text        NOT NULL,
    day           date        NOT NULL,
    symbol        text        NOT NULL,
    n_replay      integer     NOT NULL,
    n_live        integer     NOT NULL,
    n_match       integer     NOT NULL,
    covered_from  timestamptz,                    -- 실행 프로세스가 그날 판단할 수 있었던 구간 시작 (워밍업 뒤)
    detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (mode, day, symbol)
);

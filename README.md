# autocoin — 크립토 테스트베드

KRX 주식 알고리즘 매매 시스템의 선행 검증용 테스트베드. 코인 시장(Binance USDT-M Futures)에서
시그널·백테스트 로직을 먼저 검증한 뒤 같은 구조를 KRX 오더북 데이터로 이식한다.

- 계획: [PLAN.md](PLAN.md)
- Phase 0 감사: [docs/PHASE0_AUDIT.md](docs/PHASE0_AUDIT.md)
- 이전 스트래들 복제 전략: [legacy/straddle/](legacy/straddle/README.md)

## 설치

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
git config core.hooksPath .githooks
```

## 로컬 DB (PostgreSQL 18 + TimescaleDB)

Homebrew로 설치하고 `brew services start postgresql@18`로 띄운다. `postgresql.conf`에 `shared_preload_libraries = 'timescaledb'`가 있어야 한다.

```bash
.venv/bin/python -m src.storage.db init      # autocoin DB 생성 + 스키마 적용 (멱등)
```

- 접속: `postgresql:///autocoin` (환경변수 `AUTOCOIN_PG_DSN`으로 변경)
- 테이블: `market.trades`(체결), `market.book_top5`(매수/매도 5호가, 바뀔 때마다 1행), `market.collector_events`(갭·연결·시계 오프셋)
- 모두 거래소 시각 기준 1일 hypertable, 7일 지난 chunk 압축

## 체결·5호가 수집

```bash
.venv/bin/python -m src.collect_orderbook                    # EWYUSDT 무기한 → DB
.venv/bin/python -m src.collect_orderbook --duration 300 --raw-dir data/raw   # 원본 JSONL 도 저장
```

- depth `wss://fstream.binance.com/public/stream`, aggTrade `.../market/stream` (연결 2개)
- 재연결(지수 백오프), ping 20s, stale 감지(depth 30s / trade 300s), 23h 선제 재연결, depth 갭 시 스냅샷 재동기화
- 시작 시 `OBSIDIAN_AUTOJI_PATH/crypto_testbed`와 DB가 없으면 즉시 종료. 종료 시 Obsidian 일일 로그에 요약 기록
- DB 장애 시 수집은 계속하고 미적재분은 `data/spill/`에 저장 → `python -m src.storage.db replay-spill <파일>`
- 로그: `logs/collector.log`

## 정합성 검사

```bash
.venv/bin/python -m src.integrity                        # 최근 24시간
.venv/bin/python -m src.integrity --hours 72 --backfill --obsidian
```

체결 id 갭(REST 백필), 시각 역전, 무체결·5호가 결측 구간, 호가 교차, 수신 지연·시계 오프셋을 점검한다.
`--obsidian`이면 실험 노트를 쓰고 `00-index.md` 최근 실험에 등록한다.

## 구조

```
src/
  exchange/
    auth.py        API 키 로드 + HMAC 서명 (인증만)
    rest.py        REST 클라이언트 (재시도, 429/418, weight 추적)
    orderbook.py   로컬 오더북 + U/u/pu 시퀀스 검증
    ws.py          WS 수집기 (재연결, heartbeat, 갭 재동기화)
    sinks.py       JSONL / Fanout 싱크
  storage/
    schema.sql, compression.sql   TimescaleDB 스키마
    db.py          DB 생성·스키마 적용·spill 재적재 CLI
    pg_sink.py     배치 적재 싱크 (장애 시 spill)
  collect_orderbook.py  수집 CLI
  integrity.py     정합성 검사 + 체결 백필
  obsidian.py      Obsidian 작업 로그 (경로 없으면 hard fail)
  logging_setup.py      공통 로깅
  binance_data.py  klines 수집·캐시 (REST 는 exchange.rest 위임)
  broker.py        PaperBroker / BinanceBroker(실주문 비활성)
  config.py, metrics.py
legacy/straddle/   EWYUSDT 스트래들 복제 전략 (격리)
```

## 테스트

```bash
.venv/bin/python -m pytest -q tests legacy/straddle/tests
```

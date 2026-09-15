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

## 오더북/체결 수집 (M1)

```bash
.venv/bin/python -m src.collect_orderbook                               # BTCUSDT, EWYUSDT 무기한
.venv/bin/python -m src.collect_orderbook --symbols BTCUSDT --duration 300
```

- depth: `wss://fstream.binance.com/public/stream` / aggTrade: `.../market/stream` (연결 2개)
- 저장: `data/raw/<SYMBOL>/<YYYY-MM-DD>/<HH>.jsonl` (UTC 시간 단위, kind = depth/trade/snapshot/gap/conn)
- 로그: `logs/collector.log`
- 재연결(지수 백오프), heartbeat(ping 20s), stale 감지(depth 10s / trade 60s), 23h 선제 재연결,
  depth 시퀀스 갭 시 REST 스냅샷 재동기화, aggTrade id 갭 기록

## 구조

```
src/
  exchange/
    auth.py        API 키 로드 + HMAC 서명 (인증만)
    rest.py        REST 클라이언트 (재시도, 429/418, weight 추적)
    orderbook.py   로컬 오더북 + U/u/pu 시퀀스 검증
    ws.py          WS 수집기 (재연결, heartbeat, 갭 재동기화)
    sinks.py       저장소 인터페이스 (JSONL → M2 에서 TimescaleDB)
  collect_orderbook.py  수집 CLI
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

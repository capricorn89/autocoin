# pastofi 코인판 로직1 (WOO-104~106, Linear P-WOO-9)

5분 +70bp 급등 뒤 1분 OFI ÷ 분당 거래량 < −0.5 → 숏 시장가, 익절 2% · 손절 1%(ask1 판정) · 최대 5분.
BTCUSDT · ETHUSDT · SOLUSDT · EWYUSDT (USDT-M 무기한). 스펙 `spec_v2.yaml` (pastofi-crypto-1.1, sha256 동결 — v1 에서 수량만 바꿨다: 4종목 진입 명목을 BTC 0.001 ≈ $81 에 맞춘다).

진행 (사용자 결정 2026-10-26): **페이퍼 7일 → 사용자 확인 → 라이브(별도 지시)**. 페이퍼 시작 2026-10-26 04:21 UTC.

## 실행

| launchd | 내용 | 상태 |
|---|---|---|
| `com.autocoin.pastofi.paper` | 페이퍼 (실주문·API 키 없음) | 설치·실행 중 |
| `com.autocoin.pastofi.web` | 모니터링 웹 http://100.121.82.67:8790 (tailnet 전용, 127.0.0.1 도) | 설치·실행 중 |
| `com.autocoin.pastofi.live` | 실주문 `--mode live --confirm-live` | **plist 만. 설치하지 마라** |

```bash
.venv/bin/python -m src.strategies.pastofi_crypto.runner --status            # 상태
.venv/bin/python -m src.strategies.pastofi_crypto.runner --reset-halt --note "확인함"   # 정지선 수동 재개
.venv/bin/python -m src.strategies.pastofi_crypto.report --mode paper        # 성과 리포트 (results/)
.venv/bin/python -m src.strategies.pastofi_crypto.reconcile --mode paper --date 2026-10-26   # 하루 대조
.venv/bin/python -m src.strategies.pastofi_crypto.replay --check             # 백테스트 59건 일치 검증
touch data/pastofi/paper/KILL                                                # 신규 진입 즉시 금지
```

상태 폴더 `data/pastofi/<mode>/`: `state.json`(장부), `live.json`(매초, 웹용), `trades/signals/events.jsonl`(원본),
`paper_<종목>.json`(페이퍼 시뮬), `risk_<종목>.json`(RiskGuard), `reconcile/<날짜>.json`. DB 적재본은 `exec.pastofi_*`.

## 같은 코드 경로

- WS: 수집기 코드(`src/exchange/ws.py` OrderBookCollector)를 별도 연결로 띄우고 싱크만 바꾼다. 수집기 프로세스는 건드리지 않는다.
- 엔진 입력은 `feed.SymbolFeed` 하나 — 실시간과 DB 재생(`replay.py`, 매일 대조)이 같이 쓴다. 59건 거래 단위 일치(`tests/test_pastofi_crypto_feed.py`).
- 페이퍼·라이브는 `trader.Trader` 가 같고 브로커만 다르다 (PaperExchange + 실시간 5호가 뷰 ↔ BinanceFuturesBroker + closePosition 비상 손절).
- 게이트에 걸려 건너뛴 신호도 엔진은 계속 돈다 → `signals` 에 가상 결과가 남고, 대조가 거래 단위로 맞는다.

## 진입 게이트 (하나라도 걸리면 건너뛰고 사유 기록)

정지선(누적 실현 −$15) · 종목당 하루 10건 · 보유 중 · KILL · 판단 지연 > 1초 · 시계 오프셋 > 5초 ·
depth 재동기화 중 · **피처 창(662초) 안 데이터 공백**(프로세스 시작·재시작 워밍업, WS 끊김, 갭) · depth/체결 지연 > 1초.

## 알림 (텔레그램, `[pastofi PAPER]` / `[pastofi LIVE]`)

진입·청산마다, 시작·종료, WS 3분 끊김, 매일 대조(UTC 00:20), 정지선, 하루 한도, 예외 종료, 7일 요약. 실패해도 매매는 계속(재시도 1회).

## 라이브 시작 점검 (켜지 않았다)

4종목 ISOLATED·레버리지 1·one-way·multi-assets off, commissionRate 기록, 이 전략이 아닌 포지션·미체결(`pof-` 접두어 아님)이나
`com.autocoin.overnight.live` 가 있으면 시작 거부. 진입 직전 그 종목 포지션이 장부(0)와 다르면 진입 안 함.
재시작하면 열린 포지션·미체결을 대조해 이어가고, 마감이 지난 포지션은 즉시 청산, 남은 비상 손절은 정리한다.

# EWYUSDT 양매수(롱 스트래들) 복제 전략

바이낸스 USDT-M 선물 `EWYUSDT`(iShares MSCI South Korea ETF 추종 TRADIFI_PERPETUAL)로
**옵션 양매수(롱 스트래들 = ATM 콜 1 + 풋 1)** 포지션을 동적으로 복제합니다.

## 원리

실제 옵션을 사지 않고, 스트래들의 이론 델타만큼 선물 포지션을 보유·리밸런싱하면
선물 복제 포트폴리오의 손익이 옵션 양매수 손익(만기 `|S_T − K|`)을 재현합니다(블랙숄즈 복제).

```
스트래들 델타 = 2·N(d1) − 1        # ATM≈0, 깊은 ITM→+1, 깊은 OTM→−1
목표 선물 포지션 = 스트래들 델타 × contracts
```

- 매일 `strike_time`(기본 KST 08:00)의 가격을 행사가 `K`로 고정 → 0DTE 스트래들 진입
- `expiry_time`(기본 KST 15:30, 한국장 마감)에 만기 → 정산 후 **다음 진입 전까지 청산(flat)**
- 잔존기간 `τ`는 만기까지 0으로 감쇠 (`expiry_offset_days`로 익일 이상 만기도 가능)
- `rebalance_interval` 주기로 목표 델타에 맞춰 선물 포지션을 조정
- 라이브를 장중에 시작/재시작해도 진입 시각(08:00) 가격으로 `K`를 복원

## 설치

```bash
pip install -r requirements.txt
```

Python 3.11+ (zoneinfo 사용). 표준정규 CDF는 `math.erf`로 구현되어 scipy 불필요.

## 설정 (`config.yaml`)

| 키 | 의미 |
|---|---|
| `strike_time`, `strike_timezone` | 진입(행사가) 시각/타임존 (다른 시점으로 변경 가능) |
| `expiry_time` | 만기 시각 (0DTE: 당일 15:30 KST) |
| `expiry_offset_days` | 만기일 = 진입일 + 이 값 (0 = 당일, 1 = 익일 …) |
| `rebalance_interval` | **매매 빈도** (`1m`/`5m`/`1h` …) |
| `rebalance_band` | 목표-현재 델타 차이가 이 값 초과 시에만 거래 |
| `vol_mode`, `vol_value`, `vol_window` | 변동성: 과거 실현(`realized`) 또는 상수(`fixed`) |
| `contracts` | 스트래들 계약수(스케일) |
| `taker_fee_bps`, `slippage_bps` | 비용/체결 가정 |
| `execution.mode` | `paper`(시뮬) 또는 `live`(실주문) |

## 사용

### 백테스트
```bash
python -m src.backtest                 # config.yaml 사용
```
2026-03-16(상장일)~현재 데이터를 받아 시뮬, `results/`에 손익곡선·복제오차·사이클별 csv/png 생성.

### 라이브 (페이퍼, 기본)
```bash
python -m src.live --paper             # 실시간 가격으로 시뮬 매매 + 로그
python -m src.live --once              # 1틱만 실행(동작 확인용)
```

### 실주문 (opt-in)
1. `.env.example` → `.env` 복사 후 `BINANCE_API_KEY/SECRET` 입력
2. `config.yaml`의 `execution.mode: live`
3. `python -m src.live --live`

> ⚠️ 실주문 전송 코드(`BinanceBroker.rebalance_to`)는 안전을 위해 기본 비활성(`NotImplementedError`)입니다.
> 실제 자금이 집행되므로, 검토 후 직접 활성화/구현하세요.

## 테스트
```bash
pytest tests/
```

## 구조
```
src/
  config.py        설정 로드/검증
  straddle.py      블랙숄즈 수식 (델타/프리미엄/페이오프)
  binance_data.py  klines 수집·캐시, 갭 탐지, 실현변동성, 최신가
  strategy.py      ★복제 엔진 (백테스트·라이브 공용)
  broker.py        PaperBroker(시뮬) / BinanceBroker(실주문)
  backtest.py      과거 데이터 백테스트
  live.py          실시간 페이퍼/라이브 실행
  metrics.py       성과 지표·그래프
```

## 한계 / 주의
- 데이터 히스토리가 상장일(2026-03-16)부터라 백테스트 기간이 짧습니다.
- TradFi perpetual은 휴장/주말 갭이 있어, 기준 시각이 갭에 걸리면 직전 유효가로 K를 설정합니다.
- 복제오차는 리밸런스를 촘촘히 할수록(수수료↑ 대신) 줄어듭니다.

# Phase 0 — 코드베이스 감사 보고서

- 감사일: 2026-09-15
- 범위: `/Users/woojin/Desktop/autocoin` 전체 (코드 무수정, 읽기 전용 감사)
- 규모: `src/` 10개 파일 약 1,330줄, `tests/` 2개 파일 176줄

> **요약**: 현재 프로젝트는 "코인 알고리즘 매매 시스템"이라기보다
> **바이낸스 USDT-M 선물 `EWYUSDT` 1종목으로 롱 스트래들(옵션 양매수)을 델타 복제하는 단일 전략**이다.
> 데이터는 REST 1분봉(OHLCV) CSV뿐이고, 오더북·체결 데이터, 시그널 계층, DB, 로깅 체계, git이 모두 없다.
> 반면 **설정 로드/검증, 페이퍼 브로커 회계, 백테스트↔라이브 공용 엔진 구조, 실주문 차단 장치**는
> 설계가 깔끔해서 뼈대로 재사용할 가치가 있다.

---

## 1. 디렉토리 구조

| 경로 | 역할 |
|---|---|
| `PLAN.md` | 개선 계획 (이번 작업 기준 문서) |
| `README.md` | EWYUSDT 스트래들 복제 전략 설명서 |
| `config.yaml` | 전략·비용·실행 모드 설정 (단일 파일) |
| `requirements.txt` | numpy, pandas, requests, matplotlib, pyyaml (버전 하한만, 락 파일 없음) |
| `.env.example` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` 빈 템플릿 |
| `src/config.py` | YAML → dataclass 로드 + `validate()` |
| `src/binance_data.py` | Binance fapi REST 수집(klines, 마크가, exchangeInfo), CSV 캐시, 갭 탐지, 실현변동성 |
| `src/straddle.py` | 블랙숄즈 수식 (d1, 콜/풋 가격, 스트래들 델타·프리미엄·페이오프) |
| `src/strategy.py` | `StraddleReplicator` — 사이클(진입/만기) 판정, 목표 포지션 산출. 백테스트·라이브 공용 |
| `src/broker.py` | `PaperBroker`(시뮬 체결·회계) / `BinanceBroker`(실주문, 의도적으로 `NotImplementedError`) |
| `src/backtest.py` | 과거 1m 데이터 루프 백테스트, DTE 비교, CLI |
| `src/sweep.py` | rebalance_interval × band 격자 스윕, walk-forward 분할 검증 |
| `src/live.py` | REST 폴링 기반 라이브 루프(페이퍼/라이브), 상태 JSON 저장·재개 |
| `src/metrics.py` | MDD, Sharpe, 요약, CSV/PNG 리포트 |
| `tests/` | `test_straddle.py`(8개), `test_strategy.py`(9개) |
| `data/` | `EWYUSDT_1m.csv`(22.6만 행, 2026-03-16 ~ 2026-08-20), `EWYUSDT_1d.csv`(157행) |
| `results/` | 백테스트/스윕 산출물 CSV·PNG, `live_state.json`(2026-06-21 페이퍼 상태) |
| `.venv/` | **깨진 가상환경** (아래 7절 참고) |
| `.idea/`, `.pytest_cache/`, `__pycache__/`, `.DS_Store` | IDE/캐시 부산물 |
| `.claude/settings.local.json` | Claude Code 권한 설정 |

git 저장소가 아니며 `.gitignore`도 없다.

## 2. 데이터 계층

| 항목 | 현황 |
|---|---|
| 거래소 | Binance USDT-M Futures (`https://fapi.binance.com`) 단일 |
| 대상 | `EWYUSDT` (MSCI Korea ETF 추종 TradFi perpetual) 1종목 하드 고정 |
| 수집 방식 | **REST만** 사용. `/fapi/v1/klines`(페이지네이션 1500건), `/fapi/v1/premiumIndex`·`ticker/price`(라이브 폴링), `/fapi/v1/exchangeInfo`. **WebSocket 없음** |
| 재시도 | `_request()` 5회 재시도, 429 시 선형 백오프, 페이지 간 0.25s 슬립. 가중치 헤더(`X-MBX-USED-WEIGHT`) 미확인 |
| 저장소 | `data/{symbol}_{interval}.csv` 파일 캐시. DB 없음 |
| 스키마 | `ts(UTC), open, high, low, close, volume` — OHLCV만. 원본의 거래건수·taker buy volume(`trades`, `tbav`)은 **버려짐** |
| 오더북/체결 | **전혀 없음** (depth, aggTrade, bookTicker 미사용) |
| 정합성 | `find_gaps()`로 간격 > 1.5×interval 갭 탐지만 (출력용). 시퀀스 갭·타임스탬프 역전·중복 검증 없음 |
| 캐시 문제 | 캐시가 요청 구간을 못 덮으면 **전 구간 재다운로드 후 덮어쓰기**(증분 갱신 아님). 캐시 CSV에 원자적 쓰기 없음 |

## 3. 시그널 계층

- **예측형 시그널/피처는 없다.** TxnImbalance, PastReturn, LobImbalance 어느 것도 없음.
- 존재하는 "신호"는 전략 내부 계산뿐:
  - 스트래들 델타 `2·N(d1) − 1` (`straddle.py`) — 입력: 현재가, 행사가 K, 잔존기간 τ, σ
  - 실현변동성: 일봉 종가 로그수익률 rolling std × √365 (`binance_data.realized_vol_series`), 백테스트에서 직전 일자 값만 사용해 lookahead 방지 (`backtest._sigma_lookup`)
- 시그널 공통 인터페이스, IC/분위수 분석 등 **예측력 검증 도구 없음**.

## 4. 백테스트 계층

| 항목 | 현황 |
|---|---|
| 엔진 | `backtest.simulate()` — 1m 봉 **종가** 순차 루프(이벤트 기반, 파이썬 for문). 엔진 로직은 라이브와 공용(`StraddleReplicator`, `PaperBroker`) → 백테스트/라이브 괴리 적음 (장점) |
| 체결가 | 봉 종가 × (1 ± `slippage_bps`) — **고정 bps 슬리피지** |
| 수수료 | `taker_fee_bps` 편도 정률. `--maker` 플래그 시 maker 수수료 + 슬리피지 0 (지정가 미체결 위험 무시) |
| 지연 | **모델링 없음** — 판정 봉 종가에 즉시 체결 (신호·체결 동일 시점) |
| 호가/유동성 | 오더북 depth·시장충격·부분체결 없음. 수량 step 0.01, min_notional 5 반영 |
| 펀딩비 | **미반영** (perpetual 보유 비용 누락 — 1DTE 오버나잇 보유에 영향) |
| 휴장 갭 | 데이터 갭은 탐지만, 갭을 넘는 체결 불가 상황은 모델링 없음 |
| 성과 지표 | 총손익, MDD, Sharpe(봉 단위 손익 diff 기반 — 수익률이 아닌 금액 기준), 복제오차, 수수료, 거래 횟수 |
| 과적합 방지 | `sweep.py --splits` walk-forward 구간별 일관성 비교 (장점) |

## 5. 실행 계층

| 항목 | 현황 |
|---|---|
| 주문 실행 | `BinanceBroker.rebalance_to()`가 `NotImplementedError` — **실주문 경로는 사실상 없음**. 서명(HMAC), 주문 전송, 체결 확인, 주문 상태 조회 코드 없음 |
| 페이퍼 모드 | **있음, 기본값.** `live.py`는 `--live` 플래그 **와** `execution.mode: live` **둘 다** 있어야 실브로커 생성 (이중 안전장치) |
| 라이브 루프 | REST 마크가 폴링(`poll_seconds`), 예외 시 로그 출력 후 재시도, SIGINT/SIGTERM 처리 |
| 상태 | `results/live_state.json`에 포지션·현금·K 저장, 재시작 시 동일 사이클이면 재개 |
| 리스크 제한 | **없음.** 포지션 상한, 일일 손실 한도, 강제 청산, kill switch 모두 부재. 목표 포지션은 `delta × contracts`로 ±contracts 범위라 구조적으로만 제한됨 |
| 실계좌 정합 | 거래소 실제 포지션과의 reconcile 없음 |

## 6. 설정/시크릿 관리

- **하드코딩된 API 키/시크릿: 없음** — `src/`, `tests/`, `config.yaml`, `.idea/` 전수 grep 확인.
- 키는 `live.py`의 자체 `_load_env()`(`.env` 수동 파싱, `os.environ.setdefault`) → `os.getenv()`로 읽고, 없으면 `BinanceBroker` 생성자에서 즉시 예외. 실제 `.env` 파일은 현재 없음.
- 문제점:
  - `.gitignore` **없음** → git init 시 `.env`, `data/`, `results/`, `.venv/`, `.idea/`가 그대로 커밋될 위험
  - `BinanceBroker`가 `api_secret`을 인스턴스 속성으로 평문 보관 (repr/로그 노출 위험)
  - 인증 로직이 모듈로 분리돼 있지 않음 (M1 요구사항 미충족)
  - `_load_env()` 자체 구현: 따옴표·주석 처리 미흡 (`python-dotenv` 대체 권장)
- 설정 불일치: `README`는 strike 08:00/0DTE를 기본으로 설명하지만 `config.yaml`은 15:30/1DTE. `maker_fee_bps`는 dataclass에만 있고 yaml에 없음. `validate()`가 `assert` 기반이라 `python -O` 실행 시 검증이 사라짐.

## 7. 테스트/로깅

**테스트**
- pytest 17개: 블랙숄즈 수식(CDF, 델타 경계, 풋콜 패리티, 페이오프), 사이클 엔진(진입/만기/flat/τ 감쇠/리밸런스 주기/1DTE 오버나잇).
- 커버리지 공백: `broker.py`(회계·슬리피지·수수료·밴드), `binance_data.py`(네트워크 mock, 캐시, 갭), `backtest.simulate` 통합, `live.py` 상태 재개, `sweep.py` 전부.
- **현재 테스트 실행 불가**: `.venv`의 python이 `/Library/Frameworks/Python.framework/Versions/3.13/...`를 가리키는데 해당 인터프리터가 삭제됨. 또한 venv가 `~/Desktop/ApiTrading/autocoin`에서 만들어진 뒤 폴더가 이동됨. 시스템 python3엔 pytest 없음. → 감사 중 테스트는 **돌려보지 못했음**. venv 재생성 필요.

**로깅**
- `logging` 모듈 **미사용**. 전부 `print()`. 레벨·파일 핸들러·구조화 로그·로테이션 없음.
- 라이브 루프의 `except Exception`이 오류를 print만 하고 계속 재시도 → 장애가 조용히 묻힘.
- 실험 메타데이터(파라미터, 데이터 구간, 커밋 해시) 기록 없음. `results/` 파일명이 고정이라 재실행 시 덮어씀.
- Obsidian 연동 없음.

## 8. 기술 부채 분류

### 재사용 (그대로 또는 소폭 수정)

| 대상 | 근거 |
|---|---|
| `config.py` 패턴 (YAML→dataclass→validate) | 단순·타입 명확. 섹션 추가(exchange, signals, risk, obsidian)로 확장 용이. assert만 예외로 교체 |
| `PaperBroker` 회계 모델 | 포지션/현금/수수료/step·min_notional 처리 정확하고 독립적. M4 페이퍼트레이딩의 가상체결 기반으로 사용 가능 |
| "엔진은 판정, 브로커는 회계" 분리 + 백테스트·라이브 공용 엔진 구조 | M3/M4에서 시그널→가상체결→실주문 경로를 같은 코드로 태우는 목표와 정확히 일치 |
| 실주문 이중 게이트 (`--live` + `mode: live`) + 기본 `NotImplementedError` | PLAN의 "실주문 명시적 플래그, 기본 페이퍼" 원칙을 이미 충족 |
| `sweep.py` walk-forward 분할 로직 | 시그널 파라미터 검증에 재활용 가능 |
| `metrics.max_drawdown`, 리포트 저장 틀 | 범용 |
| `_request()` 재시도 골격 | REST 백필 용도로 유지, rate-limit 가중치 처리만 보강 |

### 리팩터링 필요

| 대상 | 근거 / 방향 |
|---|---|
| `binance_data.py` | 수집·캐시·지표 계산·라이브 가격조회가 한 파일에 섞임. → `exchange/rest.py`(백필), `exchange/ws.py`(M1 신규), `exchange/auth.py`(M1 신규), 지표는 signals로 이동. OHLCV에서 버린 `trades`/taker buy volume 보존 (TxnImbalance 근사에 유용) |
| `backtest.simulate()` | 스트래들 전용 로직(K, premium, payoff, theo_cum)이 루프에 하드코딩. → 전략/시그널을 주입받는 범용 루프로 분리, 체결 모델(슬리피지·수수료·**지연**·펀딩비)을 플러그인화 |
| `broker.py` | 공통 `Broker` 프로토콜 정의, 체결 모델 분리, 시크릿 보관 방식 개선, 리스크 한도 레이어 삽입 지점 마련 |
| `live.py` | REST 폴링 → WS 이벤트 기반, `print`→`logging`, 광범위 예외 삼킴 → 오류 분류·incident 기록, 자체 `.env` 파서 → python-dotenv |
| `metrics.py` | Sharpe를 금액 diff가 아닌 수익률 기준으로, IC·분위수 수익률 등 예측력 지표 추가 |
| 결과 저장 | 고정 파일명 덮어쓰기 → 실행 ID/타임스탬프 기반 + 메타데이터(파라미터, 구간, 커밋 해시) |
| `config.yaml`/`README` | 불일치 정리, EWYUSDT 하드코딩 해제 |
| 테스트 | venv 재생성, broker·data(mock)·simulate 통합 테스트 추가 |

### 버릴 부분 (또는 별도 보관)

| 대상 | 근거 |
|---|---|
| `straddle.py`, `StraddleReplicator` 사이클 로직, `run_compare_dte` | 옵션 복제 전략 전용. 목표(오더북 기반 3종 시그널 → KRX 이식)와 무관. 삭제보다는 `legacy/straddle/` 등으로 격리 후 코어에서 의존 제거 권장 |
| `data/EWYUSDT_*.csv` | 1m OHLCV는 오더북/체결 시그널 검증에 해상도·필드 부족. PastReturn 초기 실험 정도에만 참고 가능. DB로 이관 대상 아님 |
| `results/*` 기존 산출물, `live_state.json` | 스트래들 전략 결과물. 보관만 |
| `.venv/` | 깨짐. 삭제 후 재생성 |
| `__pycache__`, `.pytest_cache`, `.DS_Store` | 부산물 — `.gitignore` 대상 |

---

## 9. 갭 분석: 목표 구조 vs 현재 상태

범례: ✅ 충족 · 🟡 부분/재사용 가능 · ❌ 없음

| 마일스톤 | 목표 항목 | 현재 상태 | 판정 | 필요 작업 |
|---|---|---|---|---|
| **공통** | git + `.gitignore` + `.env.example`만 커밋 | git 없음, `.gitignore` 없음, `.env.example` 있음 | ❌ | git init, `.gitignore` 작성 (M1 착수 전 최우선) |
| 공통 | 실주문 명시적 플래그, 기본 페이퍼 | `--live` + `mode: live` 이중 게이트, 주문 전송 미구현 | ✅ | 유지 |
| 공통 | 가정(수수료·슬리피지·지연) 주석 + 결정 노트 | yaml 주석에 수수료/슬리피지만. 지연·펀딩 가정 없음, 결정 노트 없음 | 🟡 | 체결 모델 가정 문서화 |
| 공통 | 실행 가능한 개발 환경 | `.venv` 깨짐, 락 파일 없음 | ❌ | venv 재생성, 버전 고정 |
| **M1** | 거래소 선정 근거 | Binance 선택, 근거 문서 없음 | ❌ | 수수료/API 안정성/depth/rate limit 비교 결정 노트 |
| M1 | 인증 모듈 분리 | 인증 코드 자체가 없음 (키만 읽음) | ❌ | `exchange/auth.py` 신규 (HMAC 서명, 시크릿 비노출) |
| M1 | WS 오더북 스트림 (재연결/heartbeat/시퀀스 갭) | REST 폴링만 | ❌ | 신규 작성 (depth diff + 스냅샷 동기화, `U/u/pu` 시퀀스 검증) |
| M1 | REST 백필 | klines 페이지네이션·재시도 있음 | 🟡 | 재사용 + rate-limit weight 처리 |
| **M2** | TimescaleDB + Docker + 백업 정책 | CSV 파일 캐시 | ❌ | 신규 |
| M2 | 틱/오더북 스키마, hypertable 파티셔닝 기준 | OHLCV CSV 스키마만 | ❌ | 신규 설계 |
| M2 | 정합성 검증 (결측/시퀀스 갭/타임스탬프 역전) | 시간 갭 탐지 함수 1개 | 🟡 | `find_gaps` 확장 + 시퀀스·역전·중복 검사 추가 |
| M2 | Obsidian 로깅 (hard fail, 폴더 구조, frontmatter) | 없음, `print`만 | ❌ | 신규 + `logging` 체계 도입 |
| **M3** | 시그널 3종 (TxnImbalance/PastReturn/LobImbalance) | 없음 | ❌ | 신규. 입력 데이터(체결·오더북) 자체가 M1/M2 선행 필요 |
| M3 | 공통 시그널 인터페이스 | 없음 | ❌ | 신규 (`Signal.update(event) -> float` 류) |
| M3 | 예측력 검증 (IC, 분위수) 선행 | 없음 | ❌ | 신규 분석 모듈 |
| M3 | 백테스트 엔진 + 코인 데이터 연결 | 스트래들 전용 봉 루프, CSV 연결 | 🟡 | 엔진 범용화, DB 소스 연결, 지연·펀딩 모델 추가 |
| M3 | 시그널별 성능 비교 리포트 자동 생성 | 스트래들용 CSV/PNG 리포트, DTE 비교표 | 🟡 | 리포트 틀 재사용, 시그널 비교 형식으로 재설계 |
| M3 | 과적합 방지 검증 | walk-forward 스윕 있음 | 🟡 | 시그널 파라미터용으로 일반화 |
| **M4** | 페이퍼트레이딩 (시그널→가상체결 전체 경로) | `PaperBroker` + 라이브 루프 (스트래들용) | 🟡 | 시그널 파이프라인에 연결, WS 기반으로 전환 |
| M4 | 소액 실전 주문 모듈 | `NotImplementedError` | ❌ | 신규 (서명 주문, 체결 확인, 거래소 포지션 reconcile) |
| M4 | 리스크 한도 (포지션 상한/일일 손실/강제 청산) | 없음 | ❌ | 신규 리스크 레이어 (브로커 앞단) |
| M4 | 1주 검증 리뷰 리포트 | 없음 | ❌ | 신규 |
| **테스트/로깅** | 단위·통합 테스트 | 수식·사이클 17개, 현재 실행 불가 | 🟡 | 환경 복구 + 커버리지 확대 |

### 판단

- **전면 재작성은 불필요**하다고 본다. 설정·브로커 회계·엔진/브로커 분리·실주문 게이트·walk-forward는 살리고,
  스트래들 전용 로직만 격리한 뒤 그 자리에 데이터(WS/DB)·시그널·리스크 계층을 **추가**하는 형태가 맞다.
- 다만 목표 기능 대부분(WS 오더북, DB, 시그널 3종, 리스크, 인증, Obsidian)은 현재 코드에 대응물이 없어 **실질적으로는 신규 작성 비중이 크다**. "개선"의 의미는 뼈대 재사용에 가깝다는 점을 미리 공유한다.
- M1 착수 전 선결 과제: ① git init + `.gitignore` ② venv 재생성 후 기존 테스트 통과 확인 ③ 스트래들 코드 격리 여부 결정.

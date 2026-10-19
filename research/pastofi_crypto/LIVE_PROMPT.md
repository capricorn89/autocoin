# 새 세션 지시문 — pastofi 코인판 로직1 라이브 (최소 수량)

> 아래 블록을 새 세션 첫 메시지로 붙여 넣는다.

---

autocoin 저장소(`~/Desktop/autocoin`)에 **pastofi 코인판 로직1(급등 뒤 OFI 역방향 매도) 라이브 매매**를 만든다.
각 선물의 최소 수량으로 Binance USDT-M 무기한에서 실주문을 낸다. 착수 전에 아래를 다 읽고, "착수 전 확인" 항목은 나에게 먼저 물어봐라.

## 배경
- 원 전략: `~/Desktop/krx-hft/pastofi/` (KRX 주식 신호 → 주식선물 체결). 판단·체결 로직은 `pastofi/engine.py`의 `SymbolEngine`이다.
  engine.py는 1.3(ce9652f)부터 1.5(a6d3d9b)까지 같고, 파라미터 기본값만 다르다. **기본값을 믿지 말고 아래 값을 전부 명시해라.**
- 코인판 백테스트: `research/pastofi_crypto/run_all.py`. 설정 A·B 가운데 **A**, 로직1만 쓴다.
  결과는 `research/pastofi_crypto/all_trades_*.csv`이고 `th == "A"`, `logic == "로직1"`인 59건이다.
  기간은 9/24~10/18(EWY는 9/16~), 수수료 전 평균 +11.8bp, 왕복 8bp를 빼면 합계 +223bp다.
  단 10/13 하루를 빼면 −17bp로, 수수료 후 우위는 통계적으로 약하다. **이건 라이브 검증용이다.** 10/19부터가 표본 밖이다.
- 수집기: 4종목 모두 Binance USDT-M 무기한이다(`src/exchange/ws.py`, `fstream.binance.com`, depth@100ms + aggTrade). 현물은 없다.
- 기존 라이브: `src/overnight/`(EWYUSDT 밤 보유, launchd `com.autocoin.overnight.live`, 스펙 동결 yaml + sha256 테스트,
  `src/execution/{live,risk,paper}.py`의 RiskGuard). **이 구조와 관례를 최대한 재사용해라.**

## 전략 스펙 (pastofi-crypto-1.0으로 동결)
| 항목 | 값 |
|---|---|
| 종목 | BTCUSDT, ETHUSDT, SOLUSDT, EWYUSDT (USDT-M 무기한, 신호와 체결 모두 같은 계약) |
| 입력 | 수집기와 같은 소스. depth@100ms 로컬 오더북의 1호가(bid1, bidq1, ask1, askq1)가 바뀔 때마다 한 건, aggTrade(side = −1 if m else +1) |
| 시계 | 거래소 시각(depth 이벤트 `T`, aggTrade `T`), UTC 자정부터의 초. **UTC 날짜마다 엔진을 새로 만든다**(백테스트와 같게) |
| 수량 배율 | 호가 잔량과 체결량에 같이 ×1000을 곱한다(engine `on_trade`가 int로 자른다. 비율은 불변) |
| 급등 | past = (ln mid(T−1) − ln mid(T−301)) × 1e4 ≥ **70bp** |
| OFI | (T−61, T−1] 안 1호가 OFI 합(Cont·Kukanov·Stoikov). 1.3 그대로 |
| 거래량 | (T−661, T−61] 방향 있는 체결량 합 ÷ 10 |
| 진입 | OFI ÷ 분당 거래량 < **−0.5** → **매도(숏)** 시장가. 로직2(매수)는 하지 않는다 |
| 익절 / 손절 | ask1 ≤ 진입가×0.98 → 익절 / ask1 ≥ 진입가×1.01 → 손절 (A 설정: tp_bp=200, sl_bp=100). 백테스트 59건은 전부 5분 청산이었다 |
| 최대 보유 | T+300초에 reduceOnly 시장가 매수 |
| 포지션 | 종목당 하나. 청산 전엔 새 진입 없음. 4종목 동시 보유 가능 |
| 판단 시간 | UTC 하루 안에서 T ∈ [00:11:01, 23:55:00) (워밍업 661초, entry_end = 86400−300) |
| 삭제한 규칙 | VI·단일가, 가격제한 ±9.5%, 잔량 필터, 호가갭 필터, 60초 이어짐(필터가 없으니 안 걸린다) |
| 백테스트 파라미터 | `dataclasses.replace(DEFAULT, move_bp=70, tp_bp=200, sl_bp=100, session_open=0, session_close=86400, entry_start=0, entry_end=86100, fut_min_rem=0, fut_gap_ticks=10**9, fut_limit_bp=1e9, vi_pad=0)`, `fut_base` = 그날 첫 mid, `market="KOSPI"`(갭이 0이라 무관) |

엔진은 krx-hft를 import하지 말고 `src/strategies/pastofi_crypto/`에 복사해 쓴다(출처 커밋 해시를 적어 둔다). 표준 라이브러리만 쓰는 패키지다.

## 수량 ("최소 단위")
`exchangeInfo`의 LOT_SIZE stepSize와 MIN_NOTIONAL을 기준으로, **진입가에서 명목금액 ≥ MIN_NOTIONAL × 1.1을 만족하는 가장 작은 step 배수**를 쓴다.
2026-10-20 값(가격 기준) 예상:

| 종목 | step | MIN_NOTIONAL | 수량 | 명목 |
|---|---|---|---|---|
| BTCUSDT | 0.001 | $50 | 0.001 | ~$82 |
| ETHUSDT | 0.001 | $20 | 0.010 | ~$24 |
| SOLUSDT | 0.01 | $5 | 0.06 | ~$6.3 |
| EWYUSDT | 0.01 | $5 | 0.04 | ~$6.3 |

시작 시 exchangeInfo로 다시 계산해 로그에 남기고, 스펙 yaml의 상한(종목별 max_qty)을 넘으면 실행을 거부한다.

## 착수 전 확인 (나에게 물어볼 것)
1. **EWY 충돌.** 계정은 one-way 모드라 overnight 라이브의 EWY 롱과 이 전략의 EWY 숏이 상계된다.
   제안: EWY는 KST 15:20~09:05에는 진입하지 않고, 진입 직전 positionRisk의 EWY 포지션이 0이 아니면 건너뛴다. 아니면 EWY를 빼거나 서브계정을 쓴다.
2. **누적 손실 정지선.** 제안: 실현 손익 누적 −$5 → 신규 진입 중단 + 알림. 하루 최대 진입 수도 정한다(제안: 종목당 10).
3. **수량 표**(위 규칙) 그대로 갈지.
4. **페이퍼 리허설.** 라이브 전에 몇 시간 돌려 신호가 재생 결과와 같은지 볼지.

## 구현 요구
- **일치 검증(필수).** 실시간 코드 경로(WS 메시지 → 엔진)를 DB 재생으로 돌려 `all_trades_*.csv`의 A·로직1 59건을
  거래 단위로(t_sig, p_in, p_out, reason) 똑같이 재현하는 테스트를 만든다. 통과 전엔 라이브 금지.
- **스펙 동결.** `spec_v1.yaml` + sha256 테스트(overnight 방식). 바꾸려면 버전을 올린다.
- **계정 점검.** 시작 시 ISOLATED, 레버리지 1, one-way, multi-assets off를 확인하고 아니면 거부한다(BTC·ETH·SOL도 설정·확인).
  수수료율(commissionRate)도 조회해 기록한다(현재 테이커 4bp, 메이커 0).
- **주문.** 진입은 시장가 매도. 청산(익절·손절·5분)은 reduceOnly 시장가 매수. clientOrderId 규칙을 둔다.
  프로세스가 죽을 때를 대비해 **거래소 쪽 STOP_MARKET closePosition 비상 손절**(예: +1.5%)을 진입 직후 건다.
  정상 손절(1%)은 소프트웨어가 1호가 ask로 판정한다(백테스트와 같은 방식).
- **재시작 복구.** 시작 시 열린 포지션과 미체결 주문을 대조한다. 보유 시간이 지난 포지션은 즉시 청산하고, 남은 비상 손절 주문은 정리한다.
- **판단 타이밍.** 초 T 판단은 T−1까지의 체결이 다 들어온 뒤에 한다(예: 시계 오프셋을 보정한 벽시계 T+0.3초).
  지연·갭(depth 재동기화) 중에는 진입하지 않는다.
- **기록·리포트.** 체결 로그 DB(WOO-99)를 재사용한다. 거래마다 백테스트 가정가(신호 시점 bid1/ask1) 대비
  실제 체결가 슬리피지, 수수료, 순손익을 남기고, 일일 리포트와 텔레그램 알림(진입·청산·오류·정지선)을 보낸다.
  같은 날 DB 재생 결과와 라이브를 비교한 표도 만든다.
- **운영.** launchd 에이전트 `com.autocoin.pastofi.live`(overnight 관례, `--mode live --confirm-live`), 로그는 `logs/`.
  WS는 수집기와 별도 연결이다(수집기는 건드리지 않는다).
- **관례.** 키는 `~/.config/autocoin/.env`에서만 읽는다. 커밋·푸시는 자유. Linear는 전략별 새 프로젝트(인프라 이슈는 링크로 재사용).
  옵시디언 작업 로그를 남기고, 사용자 대면 텍스트는 존댓말로 쓴다.

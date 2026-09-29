# 오버나잇 효과 (KR) — 분석 재현

Linear: 프로젝트 「오버나잇 효과 (KR)」, WOO-95.
한국 장 기준 밤(15:30 → 다음 거래일 09:00 KST)에만 보유하는 전략의 근거 분석.

```bash
uv pip install --python .venv/bin/python -r requirements-research.txt
.venv/bin/python -m research.overnight.run              # 전체 재현 + 동결 수치 대조 (불일치 시 exit 1)
.venv/bin/python -m research.overnight.run --obsidian   # + Obsidian 실험 노트
.venv/bin/python -m research.overnight.ewy              # 모듈 단독 실행도 가능
```

| 모듈 | 내용 |
|---|---|
| `common.py` | 데이터 로드(캐시 우선), 성과 지표, 20% 스윙 국면 라벨, 세션 시각(KST/ET, 서머타임) |
| `gaps.py` | KOSPI200·SPY·EWY 오버나잇 vs 장중, 2010~ |
| `ewy.py` | EWYUSDT 시간대별 보유, 상장 전 EWY 대체값, 연결 시계열 국면 분해, 라이브 리스크(꼬리·체결 시각 민감도) |
| `kodex.py` | KODEX 200·TIGER 200 밤 보유, 연·월 분해, SPY 밤과 50/50 복합 |
| `run.py` | 전체 실행 + `EXPECTED` 대조 |

## 데이터

`data/overnight/` 에 CSV 캐시 (gitignore). 캐시가 있으면 네트워크 없이 같은 숫자가 나온다.
캐시를 지우면 원천에서 다시 받는데, yfinance 수정주가·배당 이력은 사후에 바뀔 수 있어 `EXPECTED` 와 어긋날 수 있다.

| 파일 | 원천 | 비고 |
|---|---|---|
| `KS200.csv` | FinanceDataReader `KS200` | yfinance `^KS200` 은 시가가 0 |
| `SPY.csv`, `EWY.csv` | yfinance | Adj Close/Close 로 시가·종가 배당 보정 |
| `069500.csv`, `102110.csv` + `_div` | yfinance 원시가 + 분배금 | FDR 은 2014-07 이후 수정주가뿐 |
| `EWYUSDT_1m.csv`, `EWYUSDT_funding.csv` | 바이낸스 (`src.binance_data`) | 상장 2026-03-16 |

데이터 종료일은 `common.END = 2026-09-28` (O0 스펙 동결 시점).

## 주요 가정

- EWYUSDT 체결가 = 해당 분 1분봉 시가. 펀딩은 창 안(진입 < t < 청산)만. 상장 전 펀딩 0
- 수수료(편도): EWYUSDT maker 2bp / taker 5bp, KODEX 1.5bp, SPY 0.5bp
- 국내 ETF 는 동시호가 체결이라 스프레드 0, 매도 거래세 면제. 분배금 과세·해외 양도세·유휴 현금 이자 미반영
- 국면 라벨(`swings`)은 사후 라벨이다. 성과 분해에만 쓰고 매매 신호로 쓰지 않는다

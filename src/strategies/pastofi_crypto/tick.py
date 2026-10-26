"""KRX 호가가격단위 (1.3 선물 호가 필터용). 표준 라이브러리만 쓴다.

맥 krx-hft의 common/tick.py 표(TICK_TABLE)를 그대로 옮긴 것이다 — 새로 만든 규칙이 아니다.
맥 tests/test_pastofi_tick.py가 두 표와 결과가 같은지 고정한다. 주식선물 호가단위는 기초주식과 같다.
경계는 '이상'이다 (2,000원은 5원 단위).
"""
from __future__ import annotations

# (하한가격, KOSPI 틱, KOSDAQ 틱) — 하한 '이상'
TICK_TABLE = [
    (0,       1,    1),
    (2_000,   5,    5),
    (5_000,   10,   10),
    (20_000,  50,   50),
    (50_000,  100,  100),
    (200_000, 500,  100),
    (500_000, 1_000, 100),
]
MARKETS = ("KOSPI", "KOSDAQ")


def tick_size(price: float, market: str) -> int:
    """가격 P(원)에 적용되는 호가단위(원). market은 'KOSPI' 또는 'KOSDAQ'."""
    if not price > 0:
        raise ValueError(f"유효하지 않은 가격: {price}")
    col = 2 if str(market).upper().startswith("KOSDAQ") else 1
    tick = TICK_TABLE[0][col]
    for row in TICK_TABLE:
        if price >= row[0]:
            tick = row[col]
        else:
            break
    return tick

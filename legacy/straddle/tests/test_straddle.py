"""스트래들 수식 정합성 테스트."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from legacy.straddle.straddle import (call_price, norm_cdf, put_price, straddle_delta,
                          straddle_payoff, straddle_price)


def test_norm_cdf_known_values():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(norm_cdf(-1.96) - 0.025) < 1e-3


def test_atm_delta_near_zero():
    # S=K 에서 스트래들 델타 = 2N(½σ√τ)-1 ≈ 0 (σ²/2 드리프트로 약간 양수).
    # 만기가 짧을수록 0 에 더 가까워진다.
    d_short = straddle_delta(100, 100, 1 / 365, 0.5, 0.0)
    d_month = straddle_delta(100, 100, 30 / 365, 0.5, 0.0)
    assert abs(d_short) < 0.02
    assert abs(d_month) < 0.1
    assert d_month > d_short >= 0  # 드리프트로 만기 길수록 더 양수


def test_deep_itm_otm_delta():
    # 깊은 ITM(콜) -> +1, 깊은 OTM -> -1 로 수렴
    assert straddle_delta(200, 100, 30 / 365, 0.5) > 0.95
    assert straddle_delta(50, 100, 30 / 365, 0.5) < -0.95


def test_delta_bounds():
    for S in (60, 80, 100, 120, 160):
        d = straddle_delta(S, 100, 10 / 365, 0.6)
        assert -1.0 <= d <= 1.0


def test_delta_at_expiry():
    assert straddle_delta(110, 100, 0, 0.5) == 1.0
    assert straddle_delta(90, 100, 0, 0.5) == -1.0
    assert straddle_delta(100, 100, 0, 0.5) == 0.0


def test_put_call_parity():
    # r=0: C - P = S - K
    S, K, tau, sig = 105, 100, 0.25, 0.4
    c = call_price(S, K, tau, sig, 0.0)
    p = put_price(S, K, tau, sig, 0.0)
    assert abs((c - p) - (S - K)) < 1e-9


def test_straddle_price_positive():
    assert straddle_price(100, 100, 0.1, 0.5) > 0


def test_payoff():
    assert straddle_payoff(120, 100) == 20
    assert straddle_payoff(80, 100) == 20

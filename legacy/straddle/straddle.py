"""블랙숄즈 기반 스트래들(콜+풋) 수식.

롱 스트래들 = ATM 콜 1 + 풋 1.
 - 콜 델타 = N(d1), 풋 델타 = N(d1) - 1
 - 스트래들 델타 = 2*N(d1) - 1   (S=K, τ>0 에서 ≈ 0; 깊은 ITM/OTM 에서 ±1)
선물로 이 델타만큼 포지션을 유지·리밸런싱하면 양매수 손익을 복제한다.

scipy 의존성을 피하기 위해 표준정규 CDF는 math.erf 로 구현한다.
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """표준정규 누적분포함수 N(x)."""
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def d1(S: float, K: float, tau: float, sigma: float, r: float = 0.0) -> float:
    """블랙숄즈 d1. tau(연), sigma(연율)는 양수 가정."""
    if tau <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        raise ValueError("d1: S,K,tau,sigma 는 양수여야 함")
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * tau) / (sigma * math.sqrt(tau))


def straddle_delta(S: float, K: float, tau: float, sigma: float, r: float = 0.0) -> float:
    """롱 스트래들 델타 = 2*N(d1) - 1.

    tau<=0 (만기)에서는 페이오프 기울기(±1, ATM 0)로 수렴시킨다.
    """
    if tau <= 0:
        if S > K:
            return 1.0
        if S < K:
            return -1.0
        return 0.0
    return 2.0 * norm_cdf(d1(S, K, tau, sigma, r)) - 1.0


def call_price(S: float, K: float, tau: float, sigma: float, r: float = 0.0) -> float:
    if tau <= 0:
        return max(S - K, 0.0)
    _d1 = d1(S, K, tau, sigma, r)
    _d2 = _d1 - sigma * math.sqrt(tau)
    return S * norm_cdf(_d1) - K * math.exp(-r * tau) * norm_cdf(_d2)


def put_price(S: float, K: float, tau: float, sigma: float, r: float = 0.0) -> float:
    if tau <= 0:
        return max(K - S, 0.0)
    _d1 = d1(S, K, tau, sigma, r)
    _d2 = _d1 - sigma * math.sqrt(tau)
    return K * math.exp(-r * tau) * norm_cdf(-_d2) - S * norm_cdf(-_d1)


def straddle_price(S: float, K: float, tau: float, sigma: float, r: float = 0.0) -> float:
    """스트래들 프리미엄(콜+풋). 진입 시점의 이론 옵션 비용."""
    return call_price(S, K, tau, sigma, r) + put_price(S, K, tau, sigma, r)


def straddle_payoff(S_T: float, K: float) -> float:
    """만기 페이오프 = |S_T - K|."""
    return abs(S_T - K)

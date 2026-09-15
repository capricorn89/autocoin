"""Binance USDT-M Futures REST 클라이언트.

 - 네트워크 오류/5xx: 지수 백오프 재시도
 - 429: Retry-After 만큼 대기 후 재시도
 - 418(IP 차단): 재시도하면 차단이 연장되므로 즉시 예외
 - 그 외 4xx: 요청 자체의 오류이므로 재시도하지 않고 예외
 - 응답 헤더 X-MBX-USED-WEIGHT-1M 을 추적해 한도 근접 시 다음 분까지 대기

가정: REQUEST_WEIGHT 한도 2400/분 (exchangeInfo.rateLimits, 2026-09-15 조회).
      weight 창은 분 단위로 초기화된다고 보고 다음 분 경계까지 대기한다.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .auth import BinanceCredentials, MissingCredentialsError, build_signed_query

log = logging.getLogger(__name__)

FAPI_BASE = "https://fapi.binance.com"
WEIGHT_LIMIT_1M = 2400


class BinanceRestError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class FuturesRestClient:
    def __init__(self, base_url: str = FAPI_BASE,
                 credentials: BinanceCredentials | None = None,
                 session: requests.Session | None = None,
                 timeout: float = 20.0, max_retries: int = 5,
                 weight_limit: int = WEIGHT_LIMIT_1M, weight_soft_ratio: float = 0.8,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.weight_limit = weight_limit
        self.weight_soft_ratio = weight_soft_ratio
        self._sleep = sleep
        self._clock = clock
        self.used_weight_1m = 0

    # ---- 공개 API ----
    def get(self, path: str, params: dict | None = None, signed: bool = False) -> Any:
        return self._send("GET", path, params, signed)

    def depth_snapshot(self, symbol: str, limit: int = 1000) -> dict:
        """오더북 스냅샷. limit=1000 은 weight 20."""
        return self.get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    # ---- 내부 ----
    def _send(self, method: str, path: str, params: dict | None, signed: bool) -> Any:
        if signed and self.credentials is None:
            raise MissingCredentialsError(f"{path}: 서명 요청에는 API 키가 필요합니다.")
        url = self.base_url + path
        last_error: str = ""
        for attempt in range(self.max_retries):
            self._throttle()
            headers: dict[str, str] = {}
            if signed:
                query, headers = build_signed_query(self.credentials, params)
            else:
                query = urlencode(params or {})
            full = f"{url}?{query}" if query else url

            try:
                r = self.session.request(method, full, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
                log.warning("REST %s 네트워크 오류(%d/%d): %s", path, attempt + 1,
                            self.max_retries, last_error)
                self._sleep(self._backoff(attempt))
                continue

            self._update_weight(r.headers)
            if r.status_code == 418:
                raise BinanceRestError(
                    f"{path}: IP 차단(418), Retry-After={r.headers.get('Retry-After')}s",
                    r.status_code, r.text)
            if r.status_code == 429:
                wait = self._retry_after(r.headers, attempt)
                log.warning("REST %s rate limit(429) — %.1fs 대기", path, wait)
                self._sleep(wait)
                last_error = "429"
                continue
            if r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                log.warning("REST %s 서버 오류 %d (%d/%d)", path, r.status_code,
                            attempt + 1, self.max_retries)
                self._sleep(self._backoff(attempt))
                continue
            if r.status_code >= 400:
                raise BinanceRestError(f"{path}: HTTP {r.status_code} {r.text[:200]}",
                                       r.status_code, r.text)
            return r.json()
        raise BinanceRestError(f"{path}: 재시도 {self.max_retries}회 초과 ({last_error})")

    def _update_weight(self, headers) -> None:
        raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        if raw is not None:
            try:
                self.used_weight_1m = int(raw)
            except ValueError:
                pass

    def _throttle(self) -> None:
        if self.used_weight_1m >= self.weight_limit * self.weight_soft_ratio:
            wait = 60.0 - (self._clock() % 60.0) + 0.5
            log.warning("REST weight %d/%d — 다음 분까지 %.1fs 대기",
                        self.used_weight_1m, self.weight_limit, wait)
            self._sleep(wait)
            self.used_weight_1m = 0

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(1.5 * (2 ** attempt), 30.0)

    def _retry_after(self, headers, attempt: int) -> float:
        try:
            return float(headers.get("Retry-After"))
        except (TypeError, ValueError):
            return self._backoff(attempt)

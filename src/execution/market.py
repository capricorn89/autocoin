"""시장 데이터 뷰: 최우선 5호가 + 체결 스트림.

기본 구현은 수집기(src.collect_orderbook)가 적재하는 DB(market.book_top5, market.trades)를 읽는다.
수집기와 같은 데이터로 페이퍼 체결을 시뮬하므로, 나중에 체결 품질을 비교할 기준도 같다.
데이터가 오래되면(stale) 예외를 던진다 — 오래된 호가로 체결을 시뮬하면 결과가 거짓이 된다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol

import psycopg

from ..storage.db import get_dsn
from .types import BookTop, TradePrint


class StaleMarketData(RuntimeError):
    """호가·체결이 max_stale_s 넘게 갱신되지 않음."""


class MarketView(Protocol):
    def book(self) -> BookTop: ...
    def trades_after(self, agg_id: int | None, since: datetime) -> list[TradePrint]: ...
    def last_price(self) -> float: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DbMarketView:
    """market 스키마에서 최신 5호가·체결을 읽는다."""

    def __init__(self, symbol: str, dsn: str | None = None, max_stale_s: float = 30.0,
                 clock: Callable[[], datetime] = _utc_now):
        self.symbol = symbol
        self.max_stale_s = max_stale_s
        self._clock = clock
        self._conn = psycopg.connect(get_dsn(dsn), autocommit=True)

    def close(self) -> None:
        self._conn.close()

    def _check(self, ts: datetime, what: str) -> None:
        age = (self._clock() - ts).total_seconds()
        if age > self.max_stale_s:
            raise StaleMarketData(f"{self.symbol} {what} 이 {age:.0f}s 전 데이터입니다 (한도 {self.max_stale_s:.0f}s)")

    def book(self) -> BookTop:
        cols = ", ".join([f"bid_px_{i}, bid_qty_{i}" for i in range(1, 6)] +
                         [f"ask_px_{i}, ask_qty_{i}" for i in range(1, 6)])
        row = self._conn.execute(
            f"SELECT exchange_ts, {cols} FROM market.book_top5 WHERE symbol = %s "
            "ORDER BY exchange_ts DESC, update_id DESC LIMIT 1", (self.symbol,)).fetchone()
        if row is None:
            raise StaleMarketData(f"{self.symbol} 5호가 없음")
        ts, v = row[0], row[1:]
        bids = [(v[2 * i], v[2 * i + 1]) for i in range(5) if v[2 * i] is not None]
        asks = [(v[10 + 2 * i], v[11 + 2 * i]) for i in range(5) if v[10 + 2 * i] is not None]
        if not bids or not asks:
            raise StaleMarketData(f"{self.symbol} 5호가 한쪽이 비어 있음")
        # 5호가는 바뀔 때만 1행이 쌓인다. 조용한 시장이면 오래돼 보일 수 있어 체결 시각도 같이 본다
        last_trade = self._conn.execute(
            "SELECT max(exchange_ts) FROM market.trades WHERE symbol = %s AND exchange_ts > %s",
            (self.symbol, ts)).fetchone()[0]
        self._check(max(ts, last_trade) if last_trade else ts, "5호가")
        return BookTop(ts=ts, bids=bids, asks=asks)

    def trades_after(self, agg_id: int | None, since: datetime) -> list[TradePrint]:
        """agg_id 이후(없으면 since 이후) 체결. 시간·id 순."""
        if agg_id is None:
            rows = self._conn.execute(
                "SELECT exchange_ts, agg_id, price, qty, is_buyer_maker FROM market.trades "
                "WHERE symbol = %s AND exchange_ts > %s ORDER BY exchange_ts, agg_id",
                (self.symbol, since)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT exchange_ts, agg_id, price, qty, is_buyer_maker FROM market.trades "
                "WHERE symbol = %s AND exchange_ts >= %s AND agg_id > %s ORDER BY exchange_ts, agg_id",
                (self.symbol, since, agg_id)).fetchall()
        return [TradePrint(*r) for r in rows]

    def last_price(self) -> float:
        row = self._conn.execute(
            "SELECT exchange_ts, price FROM market.trades WHERE symbol = %s "
            "ORDER BY exchange_ts DESC, agg_id DESC LIMIT 1", (self.symbol,)).fetchone()
        if row is None:
            raise StaleMarketData(f"{self.symbol} 체결 없음")
        self._check(row[0], "체결")
        return float(row[1])

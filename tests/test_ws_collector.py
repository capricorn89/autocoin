import asyncio
import json

from src.exchange.sinks import MemorySink
from src.exchange.ws import BookSynchronizer, CollectorConfig, OrderBookCollector


def _depth(sym, U, u, pu, b=()):
    return json.dumps({"stream": f"{sym.lower()}@depth@100ms",
                       "data": {"e": "depthUpdate", "E": 1, "s": sym, "U": U, "u": u,
                                "pu": pu, "b": list(b), "a": []}})


def _trade(sym, a):
    return json.dumps({"stream": f"{sym.lower()}@aggTrade",
                       "data": {"e": "aggTrade", "E": 1, "s": sym, "a": a, "p": "1", "q": "1"}})


class FakeWS:
    """메시지를 순서대로 돌려주고, 다 떨어지면 영원히 대기(→ stale 유도)."""

    def __init__(self, messages):
        self.messages = list(messages)

    async def recv(self):
        if self.messages:
            await asyncio.sleep(0)
            return self.messages.pop(0)
        await asyncio.sleep(3600)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fetcher(snapshots):
    calls = []

    async def fetch(symbol):
        calls.append(symbol)
        return snapshots[min(len(calls) - 1, len(snapshots) - 1)]

    return fetch, calls


def test_synchronizer_buffers_then_replays():
    async def scenario():
        sink = MemorySink()
        fetch, calls = _fetcher([{"lastUpdateId": 100, "bids": [["10", "1"]], "asks": [["11", "1"]]}])
        sync = BookSynchronizer("BTCUSDT", fetch, sink.write)
        sync.on_diff({"U": 90, "u": 95, "pu": 89, "b": [], "a": []})       # 스냅샷보다 오래됨
        sync.on_diff({"U": 96, "u": 102, "pu": 95, "b": [["10", "3"]], "a": []})
        assert not sync.synced
        await asyncio.sleep(0.01)
        assert sync.synced and sync.book.last_update_id == 102
        assert sync.book.best_bid() == (10.0, 3.0)
        sync.on_diff({"U": 103, "u": 104, "pu": 102, "b": [], "a": []})
        assert sync.book.last_update_id == 104
        assert calls == ["BTCUSDT"] and len(sink.of_kind("snapshot")) == 1

    asyncio.run(scenario())


def test_synchronizer_gap_triggers_resync():
    async def scenario():
        sink = MemorySink()
        fetch, calls = _fetcher([
            {"lastUpdateId": 100, "bids": [], "asks": []},
            {"lastUpdateId": 200, "bids": [], "asks": []},
        ])
        sync = BookSynchronizer("BTCUSDT", fetch, sink.write, retry_delay=0.001)
        sync.on_diff({"U": 99, "u": 101, "pu": 98, "b": [], "a": []})
        await asyncio.sleep(0.01)
        assert sync.synced
        sync.on_diff({"U": 150, "u": 201, "pu": 140, "b": [], "a": []})   # pu 불연속 → 갭
        assert sync.gaps == 1 and not sync.synced
        await asyncio.sleep(0.01)
        assert sync.synced and sync.book.last_update_id == 201
        assert len(calls) == 2
        assert sink.of_kind("gap")[0]["stream"] == "depth"

    asyncio.run(scenario())


def test_synchronizer_retries_when_snapshot_too_old():
    async def scenario():
        fetch, calls = _fetcher([
            {"lastUpdateId": 50, "bids": [], "asks": []},    # 버퍼 첫 이벤트보다 오래됨
            {"lastUpdateId": 105, "bids": [], "asks": []},
        ])
        sync = BookSynchronizer("BTCUSDT", fetch, MemorySink().write, retry_delay=0.001)
        sync.on_diff({"U": 100, "u": 110, "pu": 99, "b": [], "a": []})
        await asyncio.sleep(0.05)
        assert sync.synced and sync.book.last_update_id == 110
        assert len(calls) == 2

    asyncio.run(scenario())


def test_trade_gap_recorded():
    sink = MemorySink()
    fetch, _ = _fetcher([{"lastUpdateId": 1, "bids": [], "asks": []}])
    col = OrderBookCollector(CollectorConfig(symbols=["BTCUSDT"]), sink, fetch_snapshot=fetch)
    for a in (10, 11, 15):
        col.handle_message(_trade("BTCUSDT", a))
    gaps = sink.of_kind("gap")
    assert len(gaps) == 1 and gaps[0]["from_id"] == 12 and gaps[0]["to_id"] == 14
    assert len(sink.of_kind("trade")) == 3


def test_book_top_emitted_only_when_top_levels_change():
    async def scenario():
        sink = MemorySink()
        snap = {"lastUpdateId": 100,
                "bids": [[str(100 - i), "1"] for i in range(7)],     # 100..94
                "asks": [[str(101 + i), "1"] for i in range(7)]}     # 101..107
        fetch, _ = _fetcher([snap])
        col = OrderBookCollector(CollectorConfig(symbols=["EWYUSDT"], top_levels=5), sink,
                                 fetch_snapshot=fetch)
        col.handle_message(_depth("EWYUSDT", 99, 101, 98))            # 버퍼링 → 동기화
        await asyncio.sleep(0.01)
        assert not sink.of_kind("book_top")
        col.handle_message(_depth("EWYUSDT", 102, 102, 101, b=[["94", "5"]]))  # 첫 발행
        col.handle_message(_depth("EWYUSDT", 103, 103, 102, b=[["93", "2"]]))  # 6호가 밖 → 발행 안 함
        col.handle_message(_depth("EWYUSDT", 104, 104, 103, b=[["98", "7"]]))  # 3호가 수량 변경 → 발행
        col.handle_message(_depth("EWYUSDT", 105, 105, 104, b=[["98", "7"]]))  # 동일 → 발행 안 함
        tops = sink.of_kind("book_top")
        assert len(tops) == 2
        assert tops[-1]["bids"][0] == [100.0, 1.0] and tops[-1]["bids"][2] == [98.0, 7.0]
        assert len(tops[-1]["bids"]) == 5 and tops[-1]["asks"][0] == [101.0, 1.0]
        assert tops[-1]["u"] == 104

    asyncio.run(scenario())


def test_stale_stream_reconnects_and_resets_books():
    async def scenario():
        sink = MemorySink()
        fetch, calls = _fetcher([{"lastUpdateId": 100, "bids": [], "asks": []}])
        cfg = CollectorConfig(symbols=["BTCUSDT"], stale_timeout=0.05, trade_stale_timeout=60,
                              backoff_base=0.001, backoff_max=0.001)
        depth_urls, trade_urls = [], []

        def connect(url, **kw):
            if "@depth" in url:
                depth_urls.append(url)
                return FakeWS([_depth("BTCUSDT", 99, 101, 98)])
            trade_urls.append(url)
            return FakeWS([_trade("BTCUSDT", 1)])

        col = OrderBookCollector(cfg, sink, fetch_snapshot=fetch, connect=connect)
        stop = asyncio.Event()
        task = asyncio.create_task(col.run(stop))
        while len(depth_urls) < 3:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 2)

        assert depth_urls[0].startswith("wss://fstream.binance.com/public/stream?")
        assert trade_urls[0] == "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade"
        assert col.stats["depth.reconnects"] >= 2
        assert col.stats["trade.connects"] == 1 and col.stats["trade.reconnects"] == 0
        depth_conn = [r for r in sink.of_kind("conn") if r["stream"] == "depth"]
        events = [r["event"] for r in depth_conn]
        assert events[0] == "connected" and "disconnected" in events and events[-1] == "stopped"
        assert "StaleStreamError" in next(r["reason"] for r in depth_conn
                                          if r["event"] == "disconnected")
        assert len(calls) >= 2   # 재연결마다 오더북 재동기화

    asyncio.run(scenario())

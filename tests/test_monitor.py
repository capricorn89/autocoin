import logging

import requests

from src.monitor import CollectionMonitor
from src.notify import TelegramNotifier


class FakeCollector:
    def __init__(self, last=None):
        self.last_depth_at = last
        self.outages = []
        self.last_disconnect = {}


def _mon(collector, t, sent, ok=lambda: True):
    def send(text):
        if ok():
            sent.append(text)
            return True
        return False
    return CollectionMonitor(send, collector, "EWYUSDT", alert_after=180, clock=lambda: t[0])


def test_alert_once_then_recovery():
    t, sent = [1_000_000.0], []
    col = FakeCollector(last=t[0])
    m = _mon(col, t, sent)
    t[0] += 60
    m.check(); m.flush()
    assert sent == []
    t[0] += 200                                    # 260초 무수신
    col.last_disconnect["depth"] = "gaierror: [Errno 8] nodename nor servname"
    m.check(); m.check(); m.flush()
    assert len(sent) == 1 and "수집 끊김" in sent[0] and "gaierror" in sent[0]
    col.outages.append((col.last_depth_at, t[0] + 30))   # 수신 재개 → 수집기가 결측 기록
    col.last_depth_at = t[0] + 30
    t[0] += 31
    m.check(); m.flush()
    assert "수집 복구" in sent[1] and "(4분 50초)" in sent[1]


def test_gap_from_sleep_reported_on_wake():
    """잠든 동안엔 check 가 못 돌고, 깨어난 뒤 결측 기록으로 복구 알림만 간다."""
    t, sent = [2_000_000.0], []
    col = FakeCollector(last=t[0])
    m = _mon(col, t, sent)
    t[0] += 3600
    col.outages.append((t[0] - 3600, t[0]))
    col.last_depth_at = t[0]
    m.check(); m.flush()
    assert len(sent) == 1 and "수집 복구" in sent[0] and "1시간 0분" in sent[0]


def test_failed_sends_are_queued_and_retried():
    t, sent = [0.0], []
    online = [False]
    col = FakeCollector(last=0.0)
    m = _mon(col, t, sent, ok=lambda: online[0])
    t[0] = 500
    m.check(); m.flush()
    assert sent == [] and len(m.queue) == 1
    online[0] = True
    m.flush()
    assert len(sent) == 1 and not m.queue


def test_power_alerts_only_on_transition():
    t, sent = [0.0], []
    m = _mon(FakeCollector(last=0.0), t, sent)
    for code in ("ac", "battery_ok", "battery_low", "battery_low", "ac"):
        m.on_power({"code": code, "reason": f"사유-{code}"})
    m.flush()
    assert len(sent) == 2 and "해제" in sent[0] and "다시 켜짐" in sent[1]


class _Resp:
    def __init__(self, code):
        self.status_code = code


class _Session:
    def __init__(self, result):
        self.result = result

    def post(self, url, data=None, timeout=None):
        if isinstance(self.result, Exception):
            raise self.result
        return _Resp(self.result)


def test_notifier_never_logs_token(caplog):
    token = "123456:SECRET-TOKEN-VALUE"
    err = requests.ConnectionError(f"HTTPSConnectionPool: /bot{token}/sendMessage failed")
    n = TelegramNotifier(token, "42", session=_Session(err))
    with caplog.at_level(logging.DEBUG):
        assert n.send("x") is False
    assert token not in caplog.text and token not in repr(n)


def test_notifier_status_handling():
    assert TelegramNotifier("t", "1", session=_Session(200)).send("x") is True
    assert TelegramNotifier("t", "1", session=_Session(429)).send("x") is False
    assert TelegramNotifier("t", "1", session=_Session(400)).send("x") is True   # 설정 오류 → 폐기


def test_collector_records_outage():
    from src.exchange.sinks import MemorySink
    from src.exchange.ws import CollectorConfig, OrderBookCollector

    clock = [1000.0]

    async def fetch(sym):
        return {"lastUpdateId": 1, "bids": [], "asks": []}

    col = OrderBookCollector(CollectorConfig(symbols=["EWYUSDT"], outage_record_s=180), MemorySink(),
                             fetch_snapshot=fetch, clock=lambda: clock[0])
    col.last_depth_at = 1000.0
    msg = '{"data": {"e": "depthUpdate", "s": "EWYUSDT", "U": 1, "u": 2, "pu": 0, "b": [], "a": []}}'
    import asyncio

    async def feed():
        clock[0] = 1100.0
        col.handle_message(msg)
        clock[0] = 1400.0
        col.handle_message(msg)

    asyncio.run(feed())
    assert col.outages == [(1100.0, 1400.0)] and col.last_depth_at == 1400.0

import pytest

from src.exchange.auth import BinanceCredentials, MissingCredentialsError
from src.exchange.rest import BinanceRestError, FuturesRestClient


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, timeout=None):
        self.calls.append((method, url, headers))
        return self.responses.pop(0)


def _client(responses, **kw):
    sleeps = []
    sess = FakeSession(responses)
    c = FuturesRestClient(session=sess, sleep=sleeps.append, clock=lambda: 30.0, **kw)
    return c, sess, sleeps


def test_429_waits_retry_after_then_succeeds():
    c, sess, sleeps = _client([Resp(429, headers={"Retry-After": "7"}),
                               Resp(200, {"ok": 1}, {"X-MBX-USED-WEIGHT-1M": "25"})])
    assert c.get("/fapi/v1/time") == {"ok": 1}
    assert sleeps == [7.0] and c.used_weight_1m == 25


def test_client_error_not_retried():
    c, sess, _ = _client([Resp(400, {"code": -1100})])
    with pytest.raises(BinanceRestError) as ei:
        c.get("/fapi/v1/depth", {"symbol": "BAD"})
    assert ei.value.status == 400 and len(sess.calls) == 1


def test_ip_ban_raises_immediately():
    c, sess, _ = _client([Resp(418, headers={"Retry-After": "120"})])
    with pytest.raises(BinanceRestError):
        c.get("/fapi/v1/depth")
    assert len(sess.calls) == 1


def test_5xx_retries_until_limit():
    c, sess, sleeps = _client([Resp(503)] * 3, max_retries=3)
    with pytest.raises(BinanceRestError):
        c.get("/fapi/v1/depth")
    assert len(sess.calls) == 3 and len(sleeps) == 3


def test_weight_throttle_waits_until_next_minute():
    c, _, sleeps = _client([Resp(200, {}, {"X-MBX-USED-WEIGHT-1M": "2000"}), Resp(200, {})])
    c.get("/a")
    c.get("/b")
    assert sleeps == [30.5]   # clock=30s → 다음 분까지 30s + 여유 0.5s


def test_signed_request_requires_credentials_and_adds_signature():
    c, _, _ = _client([])
    with pytest.raises(MissingCredentialsError):
        c.get("/fapi/v2/account", signed=True)

    c, sess, _ = _client([Resp(200, {})], credentials=BinanceCredentials("k" * 8, "s" * 8))
    c.get("/fapi/v2/account", signed=True)
    _, url, headers = sess.calls[0]
    assert "timestamp=" in url and "signature=" in url
    assert headers["X-MBX-APIKEY"] == "k" * 8

"""pastofi 코인판 스펙 동결·엔진 출처·라이브 브로커·게이트·웹. 네트워크·DB 없음."""
import hashlib
import inspect
import json
import subprocess
import time
from pathlib import Path

import pytest

from src.strategies.pastofi_crypto import engine as eng_mod
from src.strategies.pastofi_crypto import web
from src.strategies.pastofi_crypto.brokers import PastofiLiveBroker
from src.strategies.pastofi_crypto.feed import DAY, Signal
from src.strategies.pastofi_crypto.notify import entry_text, exit_text
from src.strategies.pastofi_crypto.runner import App, Health
from src.strategies.pastofi_crypto.spec import SPEC_PATH, Spec

# spec_v1.yaml 동결 해시. 깨지면 v1 을 고친 것이다 → spec_v2.yaml 로 버전을 올릴 것
FROZEN_SHA256 = "16679636b878"
PKG = Path(eng_mod.__file__).parent
KRX = Path.home() / "Desktop" / "krx-hft"


def test_spec_v1_is_frozen():
    s = Spec.load()
    assert s.sha256.startswith(FROZEN_SHA256)
    assert s.version == "pastofi-crypto-1.0"
    assert s.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "EWYUSDT"]
    assert s.raw["risk"]["stop_cumulative_usd"] == -5.0 and s.raw["risk"]["max_entries_per_symbol_day"] == 10
    assert s.raw["paper"]["start_balance_usd"] == 200.0 and s.raw["fees_bps"]["taker"] == 4.0
    assert s.raw["live"]["emergency_stop_pct"] == 1.5 and s.raw["input"]["qty_scale"] == 1000


def test_engine_copy_is_frozen():
    got = {f: hashlib.sha256((PKG / f).read_bytes()).hexdigest()[:12] for f in ("engine.py", "params.py", "tick.py")}
    assert got == {"engine.py": "86c4304ec2ed", "params.py": "f0a072fd8a5c", "tick.py": "8070b07a629c"}


@pytest.mark.skipif(not (KRX / ".git").exists(), reason="krx-hft 저장소 없음")
def test_engine_equals_krx_hft_a6d3d9b_except_imports():
    for f in ("engine.py", "params.py", "tick.py"):
        src = subprocess.run(["git", "-C", str(KRX), "show", f"a6d3d9b:pastofi/{f}"], capture_output=True,
                             text=True, check=True).stdout
        src = src.replace("from pastofi.params import", "from .params import").replace("from pastofi.tick import", "from .tick import")
        assert src == (PKG / f).read_text(), f


# ---------------------------------------------------------------- 라이브 브로커
class FakeRest:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None, signed=False):
        self.calls.append(("GET", path, params))
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{"symbol": "SOLUSDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.0100"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"}]}]}
        raise AssertionError(path)

    def post(self, path, params=None, signed=True):
        self.calls.append(("POST", path, params))
        return {"algoId": 7, "algoStatus": "NEW", "clientAlgoId": params["clientAlgoId"]}


def test_live_emergency_stop_is_close_position_mark_price():
    c = FakeRest()
    b = PastofiLiveBroker("SOLUSDT", client=c)
    o = b.place_stop_market(__import__("src.execution.types", fromlist=["Side"]).Side.BUY, 0.06, 106.1234, "pof-SOL-x-s0")
    _, path, p = c.calls[-1]
    assert path == "/fapi/v1/algoOrder"
    assert p["closePosition"] == "true" and p["workingType"] == "MARK_PRICE" and p["type"] == "STOP_MARKET"
    assert p["triggerPrice"] == "106.12" and "quantity" not in p and "reduceOnly" not in p
    assert o.status.is_open


# ---------------------------------------------------------------- 게이트
class _App:
    """App.gate 만 쓰는 최소 객체."""
    gate = App.gate
    feeds_synced = lambda self, s: True

    def __init__(self, now):
        self.spec = Spec.load()
        self.g = self.spec.raw["gate"]
        self.lag = 0.0
        self.offset_ms = 10.0
        self._now = now
        self.health = Health(self.spec.symbols, now - 1000)
        h = self.health
        for s in self.spec.symbols:
            h.last_depth[s] = now - 0.1
            h.depth_lat[s] = h.trade_lat[s] = 50.0
        h.connected["trade"] = True

    def ex_now(self):
        return self._now


def sig(now):
    d, T = divmod(int(now), DAY)
    return Signal("BTCUSDT", d, T, T, 1.0, T + 300, {})


def test_gate_reasons():
    now = 1_790_000_000.0
    a = _App(now)
    assert a.gate("BTCUSDT", sig(now)) is None                   # 시작 1000초 뒤 → 창(662초) 밖
    a.health.point_gap("BTCUSDT", now - 100, "aggTrade id 누락")
    assert "피처 창 안 데이터 공백" in a.gate("BTCUSDT", sig(now))
    a = _App(now)
    a.lag = 2.0
    assert a.gate("BTCUSDT", sig(now)).startswith("판단 지연")
    a = _App(now)
    a.health.depth_lat["BTCUSDT"] = 1500
    assert a.gate("BTCUSDT", sig(now)) == "depth 지연 1500ms"
    a = _App(now)
    a.health.start_gap("BTCUSDT", now - 1, "depth 시퀀스 갭")
    assert a.gate("BTCUSDT", sig(now)).startswith("데이터 공백")
    a = _App(now)
    a.offset_ms = 6000
    assert a.gate("BTCUSDT", sig(now)).startswith("시계 오프셋")
    a = _App(now)
    a.health = Health(a.spec.symbols, now - 100)                 # 방금 시작 = 워밍업
    a.health.last_depth["BTCUSDT"] = now
    a.health.connected["trade"] = True
    assert "피처 창 안 데이터 공백" in a.gate("BTCUSDT", sig(now))


# ---------------------------------------------------------------- 알림 문구
def test_notification_texts():
    acc = {"balance": 201.37, "today": 0.42, "cum": 1.37, "stop": -5.0, "entries": {"SOLUSDT": 2, "ETHUSDT": 1, "BTCUSDT": 0},
           "max_entries": 10, "today_n": 3, "today_win": 2, "today_loss": 1}
    pos = [{"symbol": "BTCUSDT", "qty": 0}, {"symbol": "ETHUSDT", "qty": 0.01, "entry": 2401.45, "ask": 2400.7},
           {"symbol": "SOLUSDT", "qty": 0.06, "entry": 104.51, "ask": None}, {"symbol": "EWYUSDT", "qty": 0}]
    t = entry_text({"symbol": "SOLUSDT", "qty": 0.06, "entry_px": 104.51, "t_sig_epoch": 1792504927, "past_bp": 73.8,
                    "ofi_ratio": -1.4, "assumed_in": 104.51, "slip_in_bp": 0.0, "fee": 0.0025, "deadline_epoch": 1792505227,
                    "tp_px": 102.4198, "sl_px": 105.5551}, pos, acc)
    assert t.splitlines()[0] == "진입 SOLUSDT 숏 0.06 @ 104.51"
    assert "OFI비율 −1.4" in t and "익절 102.42" in t and "손절 105.555" in t
    assert "ETH 숏 0.01 @ 2401.45 (평가 +3.1bp)" in t and "BTC 0" in t
    assert "누적 실현 +$1.37 (정지선 −$5.00까지 $6.37)" in t and "SOL 2/10" in t
    x = exit_text({"symbol": "SOLUSDT", "reason": "5분", "qty": 0.06, "exit_px": 104.02, "entry_px": 104.51, "held_s": 300,
                   "pnl_bp": 46.9, "gross": 0.0294, "fees": 0.005, "net": 0.0244, "assumed_bp": 47.1}, pos, acc)
    assert x.splitlines()[0] == "청산 SOLUSDT 5분 · 0.06 @ 104.02"
    assert "보유 5분 0초" in x and "순 +$0.0244" in x and "차이 −0.2bp" in x and "오늘 3건 (승 2 / 패 1)" in x


# ---------------------------------------------------------------- 웹
def test_web_never_binds_all_interfaces():
    src = inspect.getsource(web.serve)
    assert '"0.0.0.0"' not in src and '(""' not in src
    assert '("127.0.0.1", port)' in src and "(tailscale_ip, port)" in src
    assert web.PORT == 8790 and web.TAILSCALE_IP == "100.121.82.67"
    assert "tailscale serve" not in inspect.getsource(web.serve)


def test_web_payload_reads_state_files(tmp_path, monkeypatch):
    d = tmp_path / "paper"
    (d / "reconcile").mkdir(parents=True)
    (d / "live.json").write_text(json.dumps({"mode": "paper", "heartbeat": time.time(), "account": {}, "symbols": {}}))
    (d / "trades.jsonl").write_text(json.dumps({"trade_id": "SOLUSDT-1", "symbol": "SOLUSDT", "t_sig": 1792504927,
                                                "exit_ts": 1792505227, "net_usd": 0.02, "gross_usd": 0.03, "fees_usd": 0.01}) + "\n")
    (d / "reconcile" / "2026-10-26.json").write_text(json.dumps(
        {"day": "2026-10-26", "ok": True, "total": {"n_replay": 1, "n_live": 1, "n_match": 1, "n_uncovered": 0, "rate": 1.0},
         "symbols": {"SOLUSDT": {"rows": []}}}))
    monkeypatch.setattr(web, "trades", lambda mode, root: web.read_jsonl(root / mode / "trades.jsonl", ("trade_id",)))
    p = web.payload("paper", tmp_path)
    assert p["state"]["age_s"] < 5 and p["trades"][0]["trade_id"] == "SOLUSDT-1"
    assert p["daily"][0]["n"] == 1 and p["reconcile"][0]["n_match"] == 1
    body = json.dumps(p)
    for secret in ("TELEGRAM", "BINANCE_API", "token"):
        assert secret not in body

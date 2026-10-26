"""pastofi 모니터링 웹 (Tailscale). krx-hft report/ofi_web.py 방식을 따른다.

    python -m src.strategies.pastofi_crypto.web            # launchd com.autocoin.pastofi.web 상시

127.0.0.1 과 이 맥의 Tailscale IP(100.121.82.67)에만 붙는다 — tailnet 안 기기만 닿는다. 0.0.0.0 에 붙이지 않는다
(같은 와이파이에 열린다). tailscale serve 는 쓰지 않는다. 부팅 직후엔 Tailscale IP 에 못 붙을 수 있어 30초마다 다시 시도한다.
포트 8790 (8787 krx-hft, 8788 다른 프로세스). 시작 전에 비어 있는지 확인한다.

매매 프로세스와 분리돼 있다(웹이 죽어도 매매는 계속). 데이터는 상태 JSON(live.json·report.json·reconcile/)과
거래 DB(exec.pastofi_*)를 읽기만 한다. DB 가 안 되면 jsonl 을 읽는다. API 키·토큰은 응답에 절대 싣지 않는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import read_jsonl

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[3]
STATE_ROOT = REPO / "data" / "pastofi"
PAGE = Path(__file__).with_name("web") / "index.html"
PORT = 8790
TAILSCALE_IP = "100.121.82.67"
MODES = ("paper", "live")
TRADE_FIELDS = ("trade_id", "symbol", "day", "t_sig", "entry_ts", "exit_ts", "qty", "entry_px", "exit_px", "assumed_in",
                "assumed_out", "reason", "gross_usd", "fees_usd", "net_usd", "pnl_bp", "assumed_bp", "slip_in_bp",
                "slip_out_bp", "past_bp", "ofi_ratio", "source")


def _epoch(x):
    if isinstance(x, datetime):
        return x.timestamp()
    return x


def trades(mode: str, root: Path = STATE_ROOT) -> list[dict]:
    try:
        import psycopg
        from ...storage.db import get_dsn
        with psycopg.connect(get_dsn(None), connect_timeout=3) as conn:
            cur = conn.execute(f"SELECT {', '.join(TRADE_FIELDS)} FROM exec.pastofi_trades WHERE mode = %s "
                               "ORDER BY t_sig", (mode,))
            rows = [dict(zip(TRADE_FIELDS, r)) for r in cur.fetchall()]
        for r in rows:
            for k in ("t_sig", "entry_ts", "exit_ts"):
                r[k] = _epoch(r[k])
            r["day"] = str(r["day"])
        return rows
    except Exception as e:
        log.warning("DB 읽기 실패 — jsonl 로 대신: %s", str(e).splitlines()[0][:120])
        rows = read_jsonl(root / mode / "trades.jsonl", ("trade_id",))
        return sorted(({k: r.get(k) for k in TRADE_FIELDS} for r in rows), key=lambda r: r["t_sig"])


def signals(mode: str, root: Path = STATE_ROOT, limit: int = 200) -> list[dict]:
    rows = read_jsonl(root / mode / "signals.jsonl", ("symbol", "t_sig"))
    keep = ("symbol", "t_sig", "status", "skip_reason", "p_in", "p_out", "reason", "engine_bp", "past_bp", "ofi_ratio")
    return sorted(({k: r.get(k) for k in keep} for r in rows), key=lambda r: r["t_sig"])[-limit:]


def reconcile(mode: str, root: Path = STATE_ROOT) -> list[dict]:
    d = root / mode / "reconcile"
    out = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        bad = [{"symbol": s, "time": row["time"], "result": row["result"], "why": row.get("why", [])}
               for s, v in r["symbols"].items() for row in v["rows"] if row["result"] not in ("일치", "미커버")]
        out.append({"day": r["day"], **r["total"], "ok": r["ok"], "issues": bad})
    return out


def daily(rows: list[dict]) -> list[dict]:
    by = defaultdict(lambda: {"n": 0, "net": 0.0, "gross": 0.0, "fees": 0.0, "win": 0})
    for t in rows:
        d = datetime.fromtimestamp(t.get("exit_ts") or t["t_sig"], tz=timezone.utc).strftime("%Y-%m-%d")
        b = by[d]
        b["n"] += 1
        b["net"] += t.get("net_usd") or 0
        b["gross"] += t.get("gross_usd") or 0
        b["fees"] += t.get("fees_usd") or 0
        b["win"] += (t.get("net_usd") or 0) > 0
    return [{"day": k, **v} for k, v in sorted(by.items())]


def state(mode: str, root: Path = STATE_ROOT) -> dict:
    p = root / mode / "live.json"
    if not p.exists():
        return {"mode": mode, "missing": True}
    s = json.loads(p.read_text())
    s["age_s"] = time.time() - s.get("heartbeat", 0)
    rp = root / mode / "report.json"
    if rp.exists() and s.get("trial", {}).get("summary_sent"):
        try:
            s["report_text"] = json.loads(rp.read_text()).get("text")
        except Exception:
            pass
    return s


def payload(mode: str, root: Path = STATE_ROOT) -> dict:
    tr = trades(mode, root)
    return {"state": state(mode, root), "trades": tr, "daily": daily(tr), "reconcile": reconcile(mode, root),
            "signals": signals(mode, root), "modes": [m for m in MODES if (root / m / "live.json").exists()],
            "server_time": time.time()}


class Handler(BaseHTTPRequestHandler):
    root = STATE_ROOT

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        mode = q.get("mode", ["paper"])[0]
        if mode not in MODES:
            mode = "paper"
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/all":
            try:
                body = json.dumps(payload(mode, self.root), ensure_ascii=False, default=str).encode()
            except Exception as e:
                log.exception("api 오류")
                body = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False).encode()
            return self._send(200, body, "application/json; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if "/api/" not in getattr(self, "path", ""):
            log.info("%s %s", self.address_string(), fmt % args)


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def serve(port: int = PORT, tailscale_ip: str = TAILSCALE_IP) -> None:
    if not port_free("127.0.0.1", port):
        raise SystemExit(f"127.0.0.1:{port} 가 이미 쓰이고 있습니다 — 다른 프로세스를 확인해 주세요.")
    local = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("pastofi 웹: http://127.0.0.1:%d", port)
    if tailscale_ip:
        def bind_tailscale():
            warned = False
            while True:
                try:
                    srv = ThreadingHTTPServer((tailscale_ip, port), Handler)
                except OSError as ex:
                    if not warned:
                        log.warning("Tailscale IP %s:%d 에 못 붙었다 — 30초마다 다시 시도한다: %s", tailscale_ip, port, ex)
                        warned = True
                    time.sleep(30)
                    continue
                log.info("pastofi 웹(tailnet): http://%s:%d", tailscale_ip, port)
                srv.serve_forever()
        threading.Thread(target=bind_tailscale, daemon=True).start()
    local.serve_forever()


def main(argv: list[str] | None = None) -> None:
    from ...logging_setup import setup_logging
    ap = argparse.ArgumentParser(prog="python -m src.strategies.pastofi_crypto.web")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--tailscale-ip", default=TAILSCALE_IP)
    ap.add_argument("--state-root", type=Path, default=STATE_ROOT)
    a = ap.parse_args(argv)
    Handler.root = a.state_root
    setup_logging("INFO", REPO / "logs" / "pastofi.web.log", console_level="WARNING")
    serve(a.port, a.tailscale_ip)


if __name__ == "__main__":
    main()

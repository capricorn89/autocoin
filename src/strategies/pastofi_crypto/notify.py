"""텔레그램 알림: 문구 만들기 + 매매를 막지 않는 전송.

전송은 별도 스레드 큐로 한다. 실패하면 로그에 남기고 5초 뒤 한 번만 다시 보낸다. 알림이 실패해도 매매는 멈추지 않는다.
머리에 [pastofi PAPER] / [pastofi LIVE] 를 붙인다 (TelegramNotifier prefix).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

SHORT = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "EWYUSDT": "EWY"}


class AsyncNotifier:
    def __init__(self, send: Callable[[str], bool] | None, retry_delay: float = 5.0):
        self._send = send
        self._q: queue.Queue = queue.Queue()
        self.retry_delay = retry_delay
        self.sent = 0
        self.failed = 0
        if send is not None:
            threading.Thread(target=self._loop, name="telegram", daemon=True).start()

    def __call__(self, text: str) -> None:
        log.info("알림: %s", text.splitlines()[0] if text else "")
        if self._send is None:
            print(text)
            return
        self._q.put(text)

    def _loop(self) -> None:
        while True:
            text = self._q.get()
            ok = self._try(text)
            if not ok:
                time.sleep(self.retry_delay)
                ok = self._try(text)
            if ok:
                self.sent += 1
            else:
                self.failed += 1
                log.error("텔레그램 전송 실패(재시도 1회 포함) — 메시지 버림: %s", text.splitlines()[0][:80])
            self._q.task_done()

    def _try(self, text: str) -> bool:
        try:
            return bool(self._send(text))
        except Exception:                                    # 전송 쪽 예외로 스레드가 죽지 않게
            log.exception("텔레그램 전송 예외")
            return False

    def drain(self, timeout: float = 10.0) -> None:
        """종료 직전 남은 메시지를 보낸다."""
        end = time.time() + timeout
        while self._send is not None and self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.1)


# ---------------------------------------------------------------- 문구
def money(x: float, plus: bool = True, nd: int = 2) -> str:
    s = f"{abs(x):.{nd}f}"
    sign = ("+" if x > 0 else "−" if x < 0 else "") if plus else ("−" if x < 0 else "")
    return f"{sign}${s}"


def sgn(x: float | None, nd: int = 1, unit: str = "") -> str:
    if x is None:
        return "-"
    return f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x):.{nd}f}{unit}"


def px(x: float) -> str:
    return f"{x:,.6g}" if x < 1000 else f"{x:,.2f}".replace(",", "")


def utc(ts: float, fmt: str = "%m/%d %H:%M:%S") -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


def dur(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60}분 {sec % 60}초"


def positions_block(rows: list[dict]) -> str:
    """rows: [{symbol, qty, entry, ask}] — qty 0 이면 '0'."""
    parts = []
    for r in rows:
        name = SHORT.get(r["symbol"], r["symbol"])
        if not r.get("qty"):
            parts.append(f"{name} 0")
            continue
        s = f"{name} 숏 {r['qty']:g} @ {px(r['entry'])}"
        if r.get("ask"):
            import math
            s += f" (평가 {sgn(math.log(r['entry'] / r['ask']) * 1e4)}bp)"
        parts.append(s)
    return "── 종목별 포지션 ──\n" + " · ".join(parts)


def account_block(acc: dict, with_counts: bool = True, with_wl: bool = False) -> str:
    left = acc["cum"] - acc["stop"]
    line = (f"잔고 {money(acc['balance'], plus=False)} · 오늘 실현 {money(acc['today'])} · "
            f"누적 실현 {money(acc['cum'])} (정지선 {money(acc['stop'])}까지 {money(left, plus=False)})")
    out = ["── 계좌 ──", line]
    if with_wl:
        out.append(f"오늘 {acc['today_n']}건 (승 {acc['today_win']} / 패 {acc['today_loss']})")
    if with_counts and acc.get("entries"):
        out.append("오늘 진입 " + " · ".join(f"{SHORT.get(s, s)} {n}/{acc['max_entries']}"
                                           for s, n in acc["entries"].items() if n))
    return "\n".join(out)


def entry_text(e: dict, positions: list[dict], acc: dict) -> str:
    lines = [
        f"진입 {e['symbol']} 숏 {e['qty']:g} @ {px(e['entry_px'])}",
        f"신호 {utc(e['t_sig_epoch'])} UTC · past {sgn(e['past_bp'])}bp · OFI비율 {sgn(e['ofi_ratio'])}",
        f"가정가 {px(e['assumed_in'])} · 슬리피지 {sgn(e['slip_in_bp'])}bp · 수수료 ${e['fee']:.4f}",
        f"최대 보유 {utc(e['deadline_epoch'], '%H:%M:%S')} · 익절 {px(e['tp_px'])} · 손절 {px(e['sl_px'])}",
    ]
    if e.get("note"):
        lines.append(e["note"])
    return "\n".join(lines + [positions_block(positions), account_block(acc)])


def exit_text(x: dict, positions: list[dict], acc: dict) -> str:
    lines = [
        f"청산 {x['symbol']} {x['reason']} · {x['qty']:g} @ {px(x['exit_px'])}",
        f"진입 {px(x['entry_px'])} → 청산 {px(x['exit_px'])} · 보유 {dur(x['held_s'])}",
        f"손익 {sgn(x['pnl_bp'])}bp ({money(x['gross'], nd=4)}) · 수수료 ${x['fees']:.4f} · 순 {money(x['net'], nd=4)}",
    ]
    if x.get("assumed_bp") is not None:
        lines.append(f"가정가 기준 {sgn(x['assumed_bp'])}bp (차이 {sgn(x['pnl_bp'] - x['assumed_bp'])}bp)")
    if x.get("note"):
        lines.append(x["note"])
    return "\n".join(lines + [positions_block(positions), account_block(acc, with_counts=False, with_wl=True)])

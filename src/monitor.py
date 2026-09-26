"""수집 끊김 감시 → 알림.

 - 호가(depth) 수신이 alert_after 초(기본 180) 넘게 없으면 '끊김' 1회
 - 수신이 돌아오면 수집기가 기록한 결측 구간(outages)으로 '복구' 알림 (결측 시작~끝, 길이)
   맥이 잠들었던 경우도 여기서 잡힌다 — 잠든 동안은 알림을 보낼 수 없고, 깨어난 뒤 복구 알림으로 알린다.
 - 수집기 시작 시 DB 마지막 호가 시각을 넘겨받아, 재부팅·프로세스 중단으로 생긴 공백도 첫 수신 때 알린다.
 - 잠자기 방지 해제(배터리 부족, 네트워크 장기 끊김)·재개를 알린다.
 - 전송 실패(네트워크 끊김 등)는 대기열에 두고 다음 주기에 재전송한다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%m-%d %H:%M:%S")


def _dur(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}시간 {m}분" if h else (f"{m}분 {s}초" if m else f"{s}초")


class CollectionMonitor:
    def __init__(self, send: Callable[[str], bool], collector, label: str,
                 alert_after: float = 180.0, interval: float = 15.0,
                 clock: Callable[[], float] = time.time, max_queue: int = 50):
        self._send = send
        self.collector = collector
        self.label = label
        self.alert_after = alert_after
        self.interval = interval
        self._clock = clock
        self._started = clock()
        self._alerted = False
        self._last_power: str | None = None
        self.queue: deque[str] = deque(maxlen=max_queue)

    def on_power(self, rec: dict) -> None:
        code = rec.get("code")
        if code in ("battery_low", "net_down") and self._last_power not in ("battery_low", "net_down"):
            self.queue.append(f"🔋 {self.label} 잠자기 방지 해제 — {rec.get('reason')}. "
                              "맥이 잠들면 수집이 멈춰요.")
        elif code in ("ac", "battery_ok") and self._last_power in ("battery_low", "net_down"):
            self.queue.append(f"🔌 {self.label} 잠자기 방지 다시 켜짐 — {rec.get('reason')}")
        self._last_power = code

    def check(self) -> None:
        now = self._clock()
        while self.collector.outages:
            start, end = self.collector.outages.pop(0)
            self.queue.append(f"✅ {self.label} 수집 복구 — 결측 {_t(start)} ~ {_t(end)} KST "
                              f"({_dur(end - start)})")
            self._alerted = False

        last = self.collector.last_depth_at
        silent_for = now - (last if last is not None else self._started)
        if silent_for >= self.alert_after and not self._alerted:
            self._alerted = True
            since = f"마지막 수신 {_t(last)} KST" if last is not None else "시작 후 수신 없음"
            reasons = [r for r in self.collector.last_disconnect.values() if r]
            why = f" 최근 끊김 사유: {reasons[-1][:120]}" if reasons else ""
            self.queue.append(f"⚠️ {self.label} 수집 끊김 — {_dur(silent_for)}째 호가 수신 없음 "
                              f"({since}).{why}")

    def flush(self) -> None:
        while self.queue:
            if not self._send(self.queue[0]):
                break
            self.queue.popleft()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.check()
            if self.queue:
                await asyncio.to_thread(self.flush)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    def final(self, text: str) -> None:
        """종료 시 마지막 알림 (남은 대기열과 함께 동기 전송)."""
        self.check()
        self.queue.append(text)
        self.flush()

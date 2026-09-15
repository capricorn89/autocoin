"""수집 레코드 저장소.

M1 은 JSONL 파일로 원본 이벤트를 그대로 남긴다. M2 에서 같은 인터페이스(write/flush/close)로
TimescaleDB 싱크를 추가해 교체한다.

레코드 kind:
 - depth    : diff depth 원본 이벤트 (+recv_ts)
 - trade    : aggTrade 원본 이벤트 (+recv_ts)
 - snapshot : REST 오더북 스냅샷 (재동기화 시점)
 - gap      : 시퀀스 갭 (depth pu 불연속, aggTrade id 불연속)
 - conn     : 연결/끊김/종료
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, TextIO


class Sink(Protocol):
    def write(self, record: dict) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class JsonlSink:
    """<root>/<SYMBOL|_system>/<YYYY-MM-DD>/<HH>.jsonl (UTC 시간 단위 로테이션, append)."""

    def __init__(self, root: str | Path, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self._clock = clock
        self._handles: dict[str, tuple[Path, TextIO]] = {}

    def _path(self, key: str, ts_ms: int) -> Path:
        t = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return self.root / key / f"{t:%Y-%m-%d}" / f"{t:%H}.jsonl"

    def write(self, record: dict) -> None:
        key = record.get("symbol") or "_system"
        ts = record.get("recv_ts") or int(self._clock() * 1000)
        path = self._path(key, ts)
        cur = self._handles.get(key)
        if cur is None or cur[0] != path:
            if cur is not None:
                cur[1].close()
            path.parent.mkdir(parents=True, exist_ok=True)
            cur = (path, path.open("a", encoding="utf-8"))
            self._handles[key] = cur
        cur[1].write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        for _, fh in self._handles.values():
            fh.flush()

    def close(self) -> None:
        for _, fh in self._handles.values():
            fh.close()
        self._handles.clear()


class FanoutSink:
    """여러 싱크에 같은 레코드를 전달 (예: DB + 원본 JSONL)."""

    def __init__(self, sinks: list):
        self.sinks = list(sinks)

    def write(self, record: dict) -> None:
        for s in self.sinks:
            s.write(record)

    def flush(self) -> None:
        for s in self.sinks:
            s.flush()

    def close(self) -> None:
        for s in self.sinks:
            s.close()


class MemorySink:
    """테스트용."""

    def __init__(self):
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def of_kind(self, kind: str) -> list[dict]:
        return [r for r in self.records if r.get("kind") == kind]

import asyncio

from src.autobackfill import AutoBackfiller
from tests.test_storage_db import BASE, _load, _trade, conn, dsn  # noqa: F401 (fixtures)


def _bf(**kw):
    t = [1_000_000.0]
    bf = AutoBackfiller("postgresql:///unused", "EWYUSDT", clock=lambda: t[0],
                        settle_s=30, startup_delay_s=60, sweep_interval_s=6 * 3600,
                        sweep_hours=72, lead_s=300, **kw)
    return bf, t


def test_due_jobs_outage_after_settle_and_sweeps():
    bf, t = _bf()
    bf.on_outage(t[0] - 1000, t[0])
    assert bf.due_jobs(t[0] + 10) == []                      # 안정화 대기 중
    jobs = bf.due_jobs(t[0] + 31)
    assert jobs == [(t[0] - 1000 - 300, t[0] + 31, "수집 재개")]
    assert bf.due_jobs(t[0] + 40) == []                      # 한 번만 실행
    sweep = bf.due_jobs(t[0] + 61)                           # 시작 1분 뒤 정기 점검
    assert sweep == [(t[0] + 61 - 72 * 3600, t[0] + 61, "정기 점검")]
    assert bf.due_jobs(t[0] + 3600) == []
    assert bf.due_jobs(t[0] + 61 + 6 * 3600)[0][2] == "정기 점검"


def test_report_only_when_something_missing():
    sent = []
    bf, _ = _bf(notify=sent.append)
    bf._report("정기 점검", {"gaps": 0, "missing": 0, "filled": 0, "remaining": 0})
    bf._report("수집 재개", {"gaps": 1, "missing": 120, "filled": 100, "remaining": 20})
    assert len(sent) == 1 and "120건 중 100건" in sent[0] and "20건 남음" in sent[0]


def test_failure_does_not_stop_loop():
    sent = []
    bf, t = _bf(notify=sent.append)
    bf.poll_s = 0.01

    def boom(start, end):
        raise RuntimeError("db down")

    bf.run_once = boom
    t[0] += 61

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(bf.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())
    assert sent and "실패" in sent[0]


def test_run_once_fills_gap(conn, dsn):
    _load(dsn, [_trade(1, BASE), _trade(2, BASE + 1_000), _trade(6, BASE + 60_000)])

    class FakeRest:
        def get(self, path, params):
            return [{"a": a, "p": "178.5", "q": "1", "f": a, "l": a, "T": BASE + 2_000 + a, "m": False}
                    for a in range(params["fromId"], 7)]

    bf = AutoBackfiller(dsn, "EWYUSDT", rest_factory=FakeRest)
    res = bf.run_once(BASE / 1000 - 600, BASE / 1000 + 600)
    assert res == {"gaps": 1, "missing": 3, "filled": 3, "remaining": 0}

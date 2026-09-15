import pytest

from src.exchange.orderbook import LocalOrderBook, SequenceGapError


def _snap(uid=100):
    return {"lastUpdateId": uid, "bids": [["10.0", "1"], ["9.9", "2"]],
            "asks": [["10.1", "1"], ["10.2", "3"]]}


def _ev(U, u, pu, b=(), a=()):
    return {"e": "depthUpdate", "E": 1, "U": U, "u": u, "pu": pu, "b": list(b), "a": list(a)}


def test_diff_before_snapshot_raises():
    with pytest.raises(RuntimeError):
        LocalOrderBook("X").apply_diff(_ev(1, 2, 0))


def test_stale_events_dropped():
    ob = LocalOrderBook("X")
    ob.apply_snapshot(_snap(100))
    assert ob.apply_diff(_ev(90, 95, 89)) is False
    assert not ob.ready


def test_first_event_must_straddle_snapshot():
    ob = LocalOrderBook("X")
    ob.apply_snapshot(_snap(100))
    assert ob.apply_diff(_ev(98, 105, 97, b=[["10.0", "5"]])) is True
    assert ob.ready and ob.last_update_id == 105
    assert ob.best_bid() == (10.0, 5.0)


def test_snapshot_older_than_first_event_is_gap():
    ob = LocalOrderBook("X")
    ob.apply_snapshot(_snap(100))
    with pytest.raises(SequenceGapError):
        ob.apply_diff(_ev(101, 110, 100))


def test_pu_chain_and_gap():
    ob = LocalOrderBook("X")
    ob.apply_snapshot(_snap(100))
    ob.apply_diff(_ev(99, 105, 98))
    ob.apply_diff(_ev(106, 110, 105))
    with pytest.raises(SequenceGapError):
        ob.apply_diff(_ev(115, 120, 112))


def test_zero_qty_removes_level_and_top():
    ob = LocalOrderBook("X")
    ob.apply_snapshot(_snap(100))
    ob.apply_diff(_ev(99, 101, 98, b=[["10.0", "0"]], a=[["10.05", "4"]]))
    assert ob.best_bid() == (9.9, 2.0)
    assert ob.best_ask() == (10.05, 4.0)
    bids, asks = ob.top(2)
    assert bids == [(9.9, 2.0)]
    assert asks == [(10.05, 4.0), (10.1, 1.0)]


def test_crossed_detection():
    ob = LocalOrderBook("X")
    ob.apply_snapshot(_snap(100))
    assert not ob.is_crossed()
    ob.apply_diff(_ev(99, 101, 98, b=[["10.5", "1"]]))
    assert ob.is_crossed()

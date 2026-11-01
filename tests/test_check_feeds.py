import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import check_feeds as cf


def _raises(exc):
    def check(_client):
        raise exc

    return check


def test_states_are_told_apart():
    checks = {
        "a": lambda c: "3 things",
        "b": lambda c: None,
        "c": _raises(cf.Unavailable("HTTP 503")),
        "d": _raises(cf.Unreadable("no features list")),
        "e": _raises(KeyError("x")),
    }
    states = {n: r["state"] for n, r in cf.run_checks(None, checks).items()}
    assert states == {"a": "ok", "b": "empty", "c": "unavailable", "d": "format", "e": "format"}


def test_format_raises_at_once_but_down_waits_for_a_streak():
    results = {"usgs": {"state": "unavailable"}, "jma": {"state": "format"}}
    streaks, fmt, stuck = cf.decide(results, {})
    assert fmt == ["jma"] and stuck == [] and streaks == {"usgs": 1, "jma": 0}
    streaks, _, stuck = cf.decide(results, {"usgs": cf.STREAK_LIMIT - 1})
    assert stuck == ["usgs"]


def test_empty_alert_feed_never_raises_but_empty_data_feed_does():
    results = {"nws": {"state": "empty"}, "usgs": {"state": "empty"}}
    streaks, fmt, stuck = cf.decide(results, {"nws": 99, "usgs": cf.STREAK_LIMIT - 1})
    assert streaks["nws"] == 0 and fmt == [] and stuck == ["usgs"]


def test_a_good_run_resets_the_streak():
    streaks, _, stuck = cf.decide({"usgs": {"state": "ok"}}, {"usgs": 2})
    assert streaks == {"usgs": 0} and stuck == []

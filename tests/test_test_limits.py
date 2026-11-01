import threading

from conftest import RSS_EXIT_CODE, watch_rss


def test_watchdog_ends_the_process_over_the_limit():
    died = threading.Event()
    codes: list[int] = []

    def die(code: int) -> None:
        codes.append(code)
        died.set()

    watch_rss(1, interval=0.01, die=die)  # one byte: any process is over it
    assert died.wait(5)
    assert codes == [RSS_EXIT_CODE]


def test_watchdog_leaves_a_process_under_the_limit_alone():
    died = threading.Event()
    watch_rss(1 << 40, interval=0.01, die=lambda code: died.set())
    assert not died.wait(0.2)

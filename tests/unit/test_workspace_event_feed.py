"""The workspace feeds show the newest events the caller may read, and read further when the newest are hidden.

A caller with limits is not shown an event that names a row above its ceiling. When the latest events of the log are all
of that kind (a confidential source captured a moment ago makes a dozen), a feed that read only its own limit would show
nothing but the caller's own telemetry. The feed widens its read, five times at a time, up to a bound.
"""
from alicebot_api.vnext_label_guard import EVENT_FEED_SCAN_LIMIT, LabelGuard


class Guard(LabelGuard):
    """A guard that admits the events marked readable, so that the feed read is tested alone."""

    def admit_events(self, rows):
        return [row for row in rows if row["readable"]]


def _log(size, *, readable):
    return [{"id": index, "readable": readable(index)} for index in range(size)]


def _feed(log, *, want):
    reads = []

    def fetch(size):
        reads.append(size)
        return log[:size]

    return Guard(store=None, active=True).newest_admitted_events(fetch, want=want), reads


def test_a_feed_with_enough_readable_events_reads_once():
    feed, reads = _feed(_log(100, readable=lambda index: True), want=20)
    assert [row["id"] for row in feed] == list(range(20))
    assert reads == [20]


def test_a_feed_reads_further_until_it_is_full_and_keeps_the_order_of_the_log():
    log = _log(5_000, readable=lambda index: index >= 60 and index % 3 == 0)
    feed, reads = _feed(log, want=20)
    assert [row["id"] for row in feed] == [index for index in range(60, 5_000) if index % 3 == 0][:20]
    assert reads == [20, 100, 500]  # the 20th readable event stands at index 117, so the second read of 100 still falls short


def test_a_feed_stops_when_the_log_is_exhausted():
    log = _log(70, readable=lambda index: index % 10 == 0)
    feed, reads = _feed(log, want=20)
    assert [row["id"] for row in feed] == [0, 10, 20, 30, 40, 50, 60]
    assert reads == [20, 100]  # the second read returned 70 of 100, so there is nothing further to read


def test_a_feed_stops_at_the_bound_even_when_the_log_goes_on():
    log = _log(EVENT_FEED_SCAN_LIMIT * 3, readable=lambda index: index >= EVENT_FEED_SCAN_LIMIT)
    feed, reads = _feed(log, want=20)
    assert feed == []
    assert EVENT_FEED_SCAN_LIMIT == 2_000  # the reach of a feed: this many events, newest first, and no more
    assert reads == [20, 100, 500, 2_000]


def test_a_feed_of_fifty_widens_in_the_same_steps():
    feed, reads = _feed(_log(10_000, readable=lambda index: index % 20 == 19), want=50)
    assert len(feed) == 50
    assert reads == [50, 250, 1250]

from datetime import timedelta

from tests.fakes import utc

NOON = utc(2026, 7, 15, 16, 20)


def test_event_queries_page_through_everything(store_factory):
    store = store_factory()
    t0 = NOON - timedelta(days=1)
    for i in range(1_050):
        store.add_event("command_accepted" if i == 3 else "settings_changed", "x", t0 + timedelta(seconds=i))
    events = store.events_since(t0)
    assert len(events) == 1_050 and events == sorted(events, key=lambda e: e["at"])
    found = store.last_event_before(t0 + timedelta(seconds=1_049), "command_accepted")
    assert found["at"] == t0 + timedelta(seconds=3)

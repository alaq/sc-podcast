import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from podcast.registry import CapacityError, Registry, continue_work, queue_work, run_tick, source_feed
from podcast.soundcloud import SourceError
from podcast.sync import Synchronizer
from conftest import Source, track


@pytest.mark.parametrize("value,expected", [
    ("https://soundcloud.com/robot-heart", "robot-heart/tracks"),
    ("soundcloud.com/KADO-NYC/likes?utm_campaign=share", "kado-nyc/likes"),
    ("https://m.soundcloud.com/user/reposts", "user/reposts"),
    ("https://soundcloud.com/user/sets/playlist", "user/sets/playlist"),
    ("https://soundcloud.com/user/a-track", "user/a-track"),
])
def test_source_url_canonicalization(value, expected):
    assert source_feed(value) == expected


@pytest.mark.parametrize("value", ["", "https://soundcloud.com/", "https://example.com/user/likes", "https://soundcloud.com@evil.example/user", "https://soundcloud.com:123/user", "https://soundcloud.com/search?q=music", "https://soundcloud.com/user/a-track?secret_token=secret", "user/sets/playlist/extra"])
def test_source_url_rejects_non_public_or_unsupported_pages(value):
    with pytest.raises(ValueError):
        source_feed(value)


def test_share_link_redirect_is_checked_before_following():
    from contextlib import nullcontext
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["allow_redirects"] is False
        return nullcontext(SimpleNamespace(status_code=302, headers={"Location": "https://127.0.0.1/private"}))
    with pytest.raises(ValueError):
        source_feed("https://on.soundcloud.com/share", SimpleNamespace(get=get))
    assert calls == ["https://on.soundcloud.com/share"]


def test_registration_is_atomic_and_cap_does_not_block_existing_feeds(config, store):
    registry = Registry(replace(config, max_auto_feeds=1), store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: registry.register("robot-heart/tracks"), range(8)))
    assert all(x == values[0] for x in values)
    with pytest.raises(CapacityError):
        registry.register("other/likes")
    assert registry.register("robot-heart/tracks") == values[0]
    assert not registry.register(config.feeds[0])["automatic"]


def test_new_feed_rate_limit_does_not_affect_repeat_subscriptions(config, store):
    registry = Registry(config, store)
    for n in range(5):
        registry.register(f"user-{n}/likes", "same-client")
    with pytest.raises(CapacityError):
        registry.register("user-6/likes", "same-client")
    assert registry.register("user-0/likes", "same-client")["feed"] == "user-0/likes"


def test_shared_tick_publishes_new_feeds_without_bypassing_existing_migration(config, store, feed):
    now = 1800000000
    registry = Registry(config, store)
    registry.register("new/tracks", now=now)
    source = Source([track(1), track(2)])
    result = run_tick(config, store, lambda: source, clock=lambda: now)
    assert result["state"] == "checked"
    assert store.manifest("new/tracks")["count"] == 2
    assert store.manifest(feed) is None
    assert store.status("new/tracks")["rollout_ready"]
    assert not store.status(feed)["rollout_ready"]
    calls = len(source.list_calls)
    run_tick(config, store, lambda: source, clock=lambda: now + 60)
    assert len(source.list_calls) == calls


def test_idle_feeds_pause_and_activity_wakes_them(config, store):
    now = 1800000000
    registry = Registry(config, store)
    registry.register("old/tracks", now=now - 31 * 86400)
    source = Source([track(1)])
    run_tick(config, store, lambda: source, clock=lambda: now)
    assert not store.manifest("old/tracks")
    registry.touch("old/tracks", now)
    run_tick(config, store, lambda: source, clock=lambda: now)
    assert store.manifest("old/tracks")["count"] == 1


def test_first_sync_failure_is_visible_and_success_recovers(config, store):
    source = Source(pages={None: SourceError("missing")})
    with pytest.raises(SourceError):
        Synchronizer(config, store, source).run("new/tracks", activate=True)
    assert store.status("new/tracks")["error"] == "sync_failed"
    source = Source([track(1)])
    Synchronizer(config, store, source).run("new/tracks", activate=True)
    assert store.manifest("new/tracks")["count"] == 1
    assert not store.status("new/tracks")["error"]


def test_queue_coalesces_subscribers_and_preview_never_dispatches(config, store):
    config = replace(config, qstash_token="test-token")
    sent = []
    client = SimpleNamespace(message=SimpleNamespace(publish_json=lambda **kw: sent.append(kw)))
    assert queue_work(config, store, "new/tracks", client=client)
    assert not queue_work(config, store, "new/tracks", client=client)
    assert len(sent) == 1 and sent[0]["body"]["bootstrap"] == 25
    assert sent[0]["url"] == config.sync_url and sent[0]["timeout"] == "60s"
    assert not queue_work(replace(config, queue_enabled=False), store, "preview/tracks", client=client)
    assert len(sent) == 1


def test_bootstrap_stops_at_budget_and_stale_job_cannot_continue(config, store, monkeypatch):
    import podcast.registry as module
    sent = []
    monkeypatch.setattr(module, "queue_work", lambda *a: sent.append(a))
    feed = "new/tracks"
    store.command("SET", store.key(feed, "queued-work"), "current")
    store.command("SET", store.key(feed, "status"), json.dumps({"has_more": True}))
    continue_work(config, store, feed, 25, "stale")
    assert not sent
    continue_work(config, store, feed, 1, "current")
    assert not sent


def test_daily_queue_budget_leaves_feed_available_for_shared_tick(config, store, monkeypatch):
    import podcast.registry as module
    monkeypatch.setattr(module.time, "time", lambda: 1800000000)
    config = replace(config, qstash_token="test-token")
    feed = "new/tracks"
    Registry(config, store).register(feed)
    key = config.namespace + ":bootstrap-budget:" + str(1800000000 // 86400)
    store.command("SET", key, "500", "EX", 90000)
    sent = []
    client = SimpleNamespace(message=SimpleNamespace(publish_json=lambda **kw: sent.append(kw)))
    assert not queue_work(config, store, feed, client=client)
    assert not sent
    assert store.command("GET", store.key(feed, "queued-work")) is None
    run_tick(config, store, lambda: Source([track(1)]), clock=lambda: 1800000000)
    assert store.manifest(feed)["count"] == 1


def test_tick_failure_does_not_block_other_sources(config, store):
    now = 1800000000
    registry = Registry(config, store)
    registry.register("broken/tracks", now=now)
    registry.register("working/tracks", now=now)
    class MixedSource(Source):
        def listing(self, feed, cursor=None):
            if feed == "broken/tracks":
                raise SourceError("Unavailable")
            return super().listing(feed, cursor)
    result = run_tick(config, store, lambda: MixedSource([track(1)]), clock=lambda: now)
    assert {"feed": "broken/tracks", "state": "retrying"} in result["feeds"]
    assert store.manifest("working/tracks")["count"] == 1
    assert store.status("broken/tracks")["error"] == "sync_failed"

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from podcast.registry import CapacityError, Registry, continue_work, queue_work, request_work, run_tick, source_feed
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


def test_tick_only_touches_main_even_with_legacy_secondary_due_entries(config, store, feed):
    now = 1800000000
    config = replace(config, feeds=(feed, "configured/tracks"))
    registry = Registry(config, store)
    for other in ("new/tracks", "configured/tracks"):
        registry.register(other, now=now)
        registry.later(other, now - 1000)  # Old deployments put these in the shared index.
    calls = []
    class MainOnlySource(Source):
        def listing(self, name, cursor=None):
            calls.append(name)
            assert name == feed
            return super().listing(name, cursor)
    source = MainOnlySource([track(1), track(2)])
    result = run_tick(config, store, lambda: source, clock=lambda: now)
    assert result["feeds"] == [{"feed": feed, "state": "prepared"}]
    assert store.manifest(feed) is None
    assert not store.status(feed)["rollout_ready"]
    assert not store.status("new/tracks") and not store.status("configured/tracks")
    assert calls == [feed]
    run_tick(config, store, lambda: source, clock=lambda: now + 60)
    assert calls == [feed]
    run_tick(config, store, lambda: source, clock=lambda: now + 31 * 86400)
    assert calls == [feed, feed]
    assert not store.status("new/tracks") and not store.status("configured/tracks")


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
    session = SimpleNamespace(post=lambda url, **kw: (sent.append((url, kw)), nullcontext(SimpleNamespace(status_code=202)))[1])
    assert queue_work(config, store, "new/tracks", session=session)
    assert not queue_work(config, store, "new/tracks", session=session)
    assert len(sent) == 1 and sent[0][1]["json"]["bootstrap"] == 25
    assert sent[0][0].endswith("/v2/publish/" + config.sync_url)
    assert sent[0][1]["timeout"] == (0.5, 1.5)
    assert sent[0][1]["headers"]["Upstash-Timeout"] == "60s"
    assert sent[0][1]["allow_redirects"] is False
    assert not queue_work(replace(config, queue_enabled=False), store, "preview/tracks", session=session)
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


def test_daily_queue_budget_requires_new_demand_after_reset(config, store, monkeypatch):
    import podcast.registry as module
    monkeypatch.setattr(module.time, "time", lambda: 1800000000)
    config = replace(config, qstash_token="test-token")
    feed = "new/tracks"
    Registry(config, store).register(feed)
    key = config.namespace + ":bootstrap-budget:" + str(1800000000 // 86400)
    store.command("SET", key, "500", "EX", 90000)
    sent = []
    monkeypatch.setattr(module.requests, "post", lambda *a, **kw: (sent.append(kw), nullcontext(SimpleNamespace(status_code=202)))[1])
    assert not request_work(config, store, feed)
    assert not sent
    assert store.command("GET", store.key(feed, "queued-work")) is None
    run_tick(config, store, lambda: Source([track(1)]), clock=lambda: 1800000000)
    assert store.manifest(feed) is None
    monkeypatch.setattr(module.time, "time", lambda: 1800000000 + 86400)
    assert not sent  # Time passing alone does not trigger work.
    assert request_work(config, store, feed)
    assert len(sent) == 1


def test_tick_records_main_failure_without_touching_secondary_sources(config, store, feed):
    now = 1800000000
    registry = Registry(config, store)
    registry.register("working/tracks", now=now)
    result = run_tick(config, store, lambda: Source(pages={None: SourceError("Unavailable")}), clock=lambda: now)
    assert result["feeds"] == [{"feed": feed, "state": "retrying"}]
    assert store.manifest("working/tracks") is None
    assert store.status(feed)["error"] == "sync_failed"


def test_demand_checks_freshness_and_coalesces_concurrent_readers(config, store, monkeypatch):
    import podcast.registry as module
    now = 1800000000
    monkeypatch.setattr(module.time, "time", lambda: now)
    config = replace(config, qstash_token="test-token")
    sent = []
    monkeypatch.setattr(module.requests, "post", lambda *a, **kw: (sent.append(kw), nullcontext(SimpleNamespace(status_code=202)))[1])
    feed = "new/tracks"
    store.command("SET", store.key(feed, "status"), json.dumps({"last_success_at": now, "last_checked_at": now}))
    assert not request_work(config, store, feed)
    assert not request_work(config, store, config.feeds[0])
    now += 1801
    assert not sent
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: request_work(config, store, feed), range(10)))
    assert sum(results) == 1 and len(sent) == 1


def test_failed_enqueue_is_throttled_and_later_demand_recovers(config, store, monkeypatch):
    import podcast.registry as module
    now = 1800000000
    monkeypatch.setattr(module.time, "time", lambda: now)
    config = replace(config, qstash_token="test-token")
    sent = []
    def post(*args, **kwargs):
        sent.append(kwargs)
        return nullcontext(SimpleNamespace(status_code=503 if len(sent) == 1 else 202))
    monkeypatch.setattr(module.requests, "post", post)
    assert not request_work(config, store, "new/tracks")
    assert store.command("GET", store.key("new/tracks", "queued-work")) is None
    assert not request_work(config, store, "new/tracks")
    assert len(sent) == 1
    now += 61
    assert request_work(config, store, "new/tracks")
    assert len(sent) == 2


def test_incomplete_archive_resumes_on_demand_before_normal_refresh_interval(config, store, monkeypatch):
    import podcast.registry as module
    now = 1800000000
    monkeypatch.setattr(module.time, "time", lambda: now)
    config = replace(config, qstash_token="test-token")
    sent = []
    monkeypatch.setattr(module, "queue_work", lambda *args: sent.append(args) or True)
    status = {"last_success_at": now - 61, "last_checked_at": now - 61, "retry_at": now - 61, "has_more": False}
    assert request_work(config, store, "new/tracks", status)
    assert len(sent) == 1

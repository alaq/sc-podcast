import copy
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from podcast.migrate import publish
from podcast.soundcloud import SourceError
from podcast.store import StorageError
from podcast.sync import Synchronizer, build_snapshot
from conftest import Source, track


def run(config, store, source, feed, now=1800000000):
    return Synchronizer(config, store, source, clock=lambda: now).run(feed)


def test_resume_200_and_publish_identical_snapshot(config, store, source, feed):
    for i in range(25):
        result = run(config, store, source, feed)
        assert result["ready"] == (i + 1) * 8
        assert store.manifest(feed) is None  # explicit migration gate
    publish(config, store, feed, no_consumer=True)
    before = store.manifest(feed)
    body = store.body(before["variants"][config.bases[0]]["key"])
    items = ET.fromstring(body).findall("./channel/item")
    assert len(items) == len({x.findtext("guid") for x in items}) == 200
    assert all(int(x.find("enclosure").get("length")) > 0 for x in items)
    assert all("\x01" not in ET.tostring(x).decode() for x in items)
    assert run(config, store, source, feed, 1800000060)["state"] == "unchanged"
    assert store.status(feed)["last_success_at"] == 1800000060
    assert store.manifest(feed) == before
    assert len(source.audio_calls) == 200
    # Serving/unchanged checks do not transfer the large state or snapshot body.
    store.calls.clear()
    assert run(config, store, source, feed)["state"] == "unchanged"
    assert not any(c[0] == "GET" and (c[1].endswith(":state") or ":body:" in c[1]) for c in store.calls)


def test_bad_track_does_not_block_good_tracks_and_can_recover(config, store, feed):
    config = replace(config, max_items=3)
    source = Source([track(1), track(2), track(3), track(4)])
    source.bad.add("4")
    assert run(config, store, source, feed)["ready"] == 3
    publish(config, store, feed, no_consumer=True)
    prior = store.manifest(feed)
    assert run(config, store, source, feed, 1800000010)["state"] == "unchanged"
    source.bad.clear()
    assert run(config, store, source, feed, 1800000901)["state"] == "published"
    assert store.manifest(feed) != prior
    assert store.state(feed)["tracks"]["4"]["length"]


def test_discovery_survives_process_death_and_old_snapshot_survives_source_failure(config, store, feed, monkeypatch):
    source = Source([track(1)])
    run(config, store, source, feed)
    publish(config, store, feed, no_consumer=True, minimum=1)
    before = store.manifest(feed)
    source.pages[None]["entries"].append(track(2))
    def killed(entry):
        raise SystemExit("simulated function kill")
    with monkeypatch.context() as patch:
        patch.setattr(source, "resolve_audio", killed)
        with pytest.raises(SystemExit):
            run(config, store, source, feed)
    assert "2" in store.state(feed)["tracks"]
    assert store.manifest(feed) == before
    assert run(config, store, source, feed)["state"] == "published"
    after = store.manifest(feed)
    source.pages[None] = SourceError("source down")
    with pytest.raises(SourceError):
        run(config, store, source, feed)
    assert store.manifest(feed) == after


def test_expired_lock_cannot_publish_or_release_new_owner(config, store, feed):
    old = store.acquire(feed)
    store.redis.delete(store.key(feed, "lock"))
    new = store.acquire(feed)
    with pytest.raises(StorageError):
        store.commit(feed, old, {"bad": True}, state={"bad": True}, manifest={"bad": True})
    assert store.state(feed) == {}
    assert store.manifest(feed) is None
    store.release(feed, old)
    assert store.redis.get(store.key(feed, "lock")) == new


def test_gap_larger_than_page_caught_up_with_durable_cursor(config, store, feed):
    source = Source([track(1)])
    run(config, store, source, feed)
    source.pages = {
        None: {"entries": [track(i) for i in range(402, 202, -1)], "next": "page2", "title": "kado"},
        "page2": {"entries": [track(i) for i in range(202, 2, -1)], "next": "page3", "title": "kado"},
        "page3": {"entries": [track(2), track(1)], "next": None, "title": "kado"},
    }
    run(config, store, source, feed)
    assert store.state(feed)["pending_pages"] == ["page3"]
    run(config, store, source, feed)
    assert set(store.state(feed)["tracks"]) == {str(i) for i in range(1, 403)}


def test_seeded_state_still_backfills_and_failure_does_not_lose_cursor(config, store, feed):
    token = store.acquire(feed)
    store.commit(feed, token, {}, state={"tracks": {}, "bootstrap_at": 1800000000, "rollout_ready": False, "legacy_seeds": {}})
    store.release(feed, token)
    source = Source(pages={None: {"entries": [track(2)], "next": "older", "title": "kado"}, "older": {"entries": [track(1)], "next": None, "title": "kado"}})
    run(config, store, source, feed)
    assert set(store.state(feed)["tracks"]) == {"1", "2"}


def test_empty_listing_keeps_last_good_feed(config, store, source, feed):
    run(config, store, source, feed)
    publish(config, store, feed, no_consumer=True, minimum=8)
    source.pages[None]["entries"] = []
    run(config, store, source, feed)
    assert store.manifest(feed)["count"] >= 8


def test_storage_failure_cannot_replace_public_snapshot(config, store, source, feed, monkeypatch):
    run(config, store, source, feed)
    publish(config, store, feed, no_consumer=True, minimum=8)
    before = store.manifest(feed)
    original = store.command
    def fail_publish(*args):
        if args[0] == "EVAL" and store.key(feed, "manifest") in args:
            raise StorageError("network unavailable")
        return original(*args)
    monkeypatch.setattr(store, "command", fail_publish)
    with pytest.raises(StorageError):
        run(config, store, source, feed)
    assert store.manifest(feed) == before


def test_current_and_previous_bodies_are_retained(config, store, feed):
    state = {"tracks": {"1": track(1, length=100, published_at=1700000000)}}
    prior = None
    snapshots = []
    for n in range(3):
        state["tracks"]["1"]["title"] = f"Title {n}"
        manifest, bodies, retire = build_snapshot(store, config, feed, state, prior, 1800000000 + n)
        snapshots.append(manifest)
        prior = copy.deepcopy(manifest)
    assert set(retire) == {x["key"] for x in snapshots[0]["variants"].values()}
    assert not set(retire) & {x["key"] for x in snapshots[1]["variants"].values()}

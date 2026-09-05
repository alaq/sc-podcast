import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from podcast.cli import schedule
from podcast.feed import enclosure_url, render
from podcast.migrate import publish, seed
from podcast.store import StorageError
from podcast.sync import Synchronizer, migration_entries
from conftest import Source, track


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata VALUES ('initialized', '2026-07-01');
        CREATE TABLE feed_items(item_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        source_url TEXT NOT NULL, published_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('baseline','pending','announced')),
        discovered_at TEXT NOT NULL, announced_at TEXT);""")
        for n, status in [(2, "pending"), (3, "announced")]:
            conn.execute("INSERT INTO feed_items VALUES (?, 'old title', 'url', 'date', ?, 'then', ?)",
                         (f"https://podcast.alaq.io/track/dj/set-{n}", status, "earlier" if status == "announced" else None))
    return path


def test_seed_preserves_existing_identity_and_dates_per_origin(config, store, feed):
    documents = {}
    for base in config.bases:
        root = ET.fromstring(render(feed, [track(1, length=123, published_at=1700000030)], base))
        item = root.find("./channel/item")
        item.remove(item.find("guid"))  # main had no GUID; enclosure is fallback identity
        documents[base] = ET.tostring(root)
    seed(config, store, feed, documents)
    source = Source([track(1)])
    Synchronizer(config, store, source).run(feed)
    saved = store.state(feed)["tracks"]["1"]
    assert saved["published_at"] == 1700000030
    assert saved["legacy_identity"]
    for base in config.bases:
        item = ET.fromstring(render(feed, [saved], base)).find("./channel/item")
        assert item.findtext("guid") == enclosure_url(base, saved)
        assert item.find("enclosure").get("url") == enclosure_url(base, saved)


def test_migration_baselines_only_historical_without_touching_pending(database, tmp_path, config, store, feed):
    entries = [track(1), track(2), track(3), track(4, legacy_identity=True), track(5, liked_at=1900000000)]
    source = Source(entries)
    Synchronizer(config, store, source, clock=lambda: 1800000000).run(feed)
    state = store.state(feed)
    assert {x["id"] for x in migration_entries(state, 200)} == {"1", "2", "3"}
    result = publish(config, store, feed, database, tmp_path / "backup", minimum=5)
    assert result["baselined"] == 1
    assert result["counts"] == {"baseline": 1, "pending": 1, "announced": 1}
    with sqlite3.connect(database) as conn:
        pending = conn.execute("SELECT title, status, announced_at FROM feed_items WHERE item_id LIKE '%set-2'").fetchone()
        assert pending == ("old title", "pending", None)
    backup = Path(result["backup"])
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 2
    again = publish(config, store, feed, database, tmp_path / "backup", minimum=5)
    assert again["baselined"] == 0
    assert store.manifest(feed)["count"] == 5


def test_baseline_failure_keeps_feed_hidden(database, tmp_path, config, store, source, feed, monkeypatch):
    Synchronizer(config, store, source).run(feed)
    def fail(*args):
        raise sqlite3.OperationalError("disk failure")
    monkeypatch.setattr("podcast.migrate.baseline", fail)
    with pytest.raises(sqlite3.OperationalError):
        publish(config, store, feed, database, tmp_path, minimum=8)
    assert not store.state(feed)["rollout_ready"]
    assert store.manifest(feed) is None


def test_unavailable_historical_tracks_are_baselined_before_later_recovery(database, tmp_path, config, store, feed):
    source = Source([track(1), track(4)])
    source.bad.add("4")
    Synchronizer(config, store, source, clock=lambda: 1800000000).run(feed)
    assert not store.state(feed)["tracks"]["4"].get("length")
    publish(config, store, feed, database, tmp_path / "backups", minimum=1)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT status FROM feed_items WHERE item_id LIKE '%set-4'").fetchone() == ("baseline",)


def test_publish_failure_after_baseline_is_safe_to_retry(database, tmp_path, config, store, feed, monkeypatch):
    Synchronizer(config, store, Source([track(1)])).run(feed)
    def fail(*args, **kwargs):
        raise StorageError("Redis down")
    with monkeypatch.context() as patch:
        patch.setattr(store, "commit", fail)
        with pytest.raises(StorageError):
            publish(config, store, feed, database, tmp_path / "backups", minimum=1)
    assert store.manifest(feed) is None
    assert publish(config, store, feed, database, tmp_path / "backups", minimum=1)["baselined"] == 0


def test_schedule_requires_prepared_feed_and_uses_stable_id(config, store, source, feed):
    config = replace(config, qstash_token="token", signing_key="current", next_signing_key="next")
    calls = []
    api = SimpleNamespace(create=lambda **kw: calls.append(kw), get=lambda ident: SimpleNamespace(destination=config.sync_url, cron="*/5 * * * *", paused=False))
    client = SimpleNamespace(schedule=api)
    with pytest.raises(ValueError):
        schedule(config, store, feed, "create", client)
    Synchronizer(config, store, source).run(feed)
    publish(config, store, feed, no_consumer=True, minimum=8)
    a = schedule(config, store, feed, "create", client)
    b = schedule(config, store, feed, "create", client)
    assert a == b and calls[0] == calls[1]
    assert calls[0]["retries"] == 2 and calls[0]["timeout"] == "60s"
    assert "headers" not in calls[0]  # QStash signature, no forwarded bearer credential

"""Explicit, idempotent migration; this module never imports a message sender."""

import fcntl
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from podcast.config import DEFAULT_FEED
from podcast.feed import enclosure_url, title_for
from podcast.store import StorageError
from podcast.sync import build_snapshot, migration_entries, ready_entries


def legacy_seeds(documents):
    seeds = {}
    for base, body in documents.items():
        items = ET.fromstring(body).findall("./channel/item")
        if not items:
            raise ValueError("Legacy feed contains no episodes")
        for item in items:
            enclosure = item.find("enclosure")
            if enclosure is None:
                raise ValueError("Legacy episode has no enclosure")
            url = enclosure.get("url", "")
            parsed = urlsplit(url)
            if parsed.netloc != urlsplit(base).netloc or not parsed.path.startswith("/track/"):
                raise ValueError("Legacy enclosure has an unexpected origin or path")
            path = unquote(parsed.path[len("/track/"):])
            date = parsedate_to_datetime(item.findtext("pubDate", ""))
            if date.tzinfo is None:
                raise ValueError("Legacy publication date has no timezone")
            seed = seeds.setdefault(path, {"enclosure_path": path, "published_at": int(date.timestamp()),
                                           "legacy_identity": True, "guid_by_base": {}})
            seed["guid_by_base"][base] = item.findtext("guid") or url
    return seeds


def seed(config, store, feed, documents=None):
    if documents is None:
        documents = {}
        for base in config.bases:
            url = base + ("/" if feed == DEFAULT_FEED else "/" + feed)
            response = requests.get(url, timeout=(3, 25), headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            documents[base] = response.content
    seeds = legacy_seeds(documents)
    token = store.acquire(feed)
    if not token:
        raise StorageError("A sync is running; retry migration later")
    try:
        state = store.state(feed)
        if state.get("rollout_ready"):
            raise ValueError("Feed is already published; do not reseed it")
        state.setdefault("tracks", {})
        state.setdefault("bootstrap_at", int(time.time()))
        state.setdefault("rollout_ready", False)
        # Re-running captures episodes added to the old live feed during preparation.
        state.setdefault("legacy_seeds", {}).update(seeds)
        for track in state["tracks"].values():
            if track["enclosure_path"] in seeds:
                track.update(seeds[track["enclosure_path"]])
        store.commit(feed, token, store.status(feed), state=state)
        return {"seeded": len(seeds), "bootstrap_at": state["bootstrap_at"]}
    finally:
        store.release(feed, token)


@contextmanager
def worker_lock(database):
    # Same file and flock protocol as podcast_channel_announce.py.
    with (Path(database).parent / "run.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def baseline(database, backup_dir, entries, feed, base):
    """Caller holds the worker lock until the corresponding Redis publication."""
    database, backup_dir = Path(database), Path(backup_dir)
    if not database.is_file():
        raise ValueError("Announcement database must already exist")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-announcements.sqlite")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with sqlite3.connect(database, timeout=10) as conn:
        if not conn.execute("SELECT value FROM metadata WHERE key='initialized'").fetchone():
            raise ValueError("Announcement database has not been initialized")
        with sqlite3.connect(backup) as target:
            conn.backup(target)
        conn.execute("BEGIN IMMEDIATE")
        before = conn.total_changes
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany("""INSERT OR IGNORE INTO feed_items
            (item_id, title, source_url, published_at, status, discovered_at)
            VALUES (?, ?, ?, ?, 'baseline', ?)""", [
            (enclosure_url(base, x), title_for(x, feed), x["webpage_url"],
             format_datetime(datetime.fromtimestamp(x["published_at"], timezone.utc), usegmt=True), now)
            for x in entries
        ])
        inserted = conn.total_changes - before
        conn.commit()
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM feed_items GROUP BY status"))
    return {"baselined": inserted, "counts": counts, "backup": str(backup)}


def publish(config, store, feed, database=None, backup_dir=None, no_consumer=False, minimum=None):
    if not database and not no_consumer:
        raise ValueError("Supply the announcement database, or explicitly declare no announcement consumer")
    if database and (feed != DEFAULT_FEED or "https://podcast.alaq.io" not in config.bases):
        raise ValueError("This announcement migration only supports the ACSv3 custom-domain feed")
    token = store.acquire(feed)
    if not token:
        raise StorageError("A sync is running; retry publication later")
    try:
        state = store.state(feed)
        entries = ready_entries(state, config.max_items)
        if len(entries) < (minimum if minimum is not None else config.max_items):
            raise ValueError("Not enough prepared episodes to publish")
        result = {}
        def commit():
            now = int(time.time())
            state["rollout_ready"] = True
            manifest, bodies, _ = build_snapshot(store, config, feed, state, store.manifest(feed), now)
            status = store.status(feed)
            status.update(rollout_ready=True, published_count=len(entries))
            if manifest:
                status["last_published_at"] = now
            store.commit(feed, token, status, state=state, manifest=manifest, bodies=bodies)
            result.update(published=len(entries))
        if database:
            with worker_lock(database):
                result.update(baseline(database, backup_dir or "migration-data", migration_entries(state, config.max_items), feed, "https://podcast.alaq.io"))
                commit()
        else:
            commit()
        return result
    finally:
        store.release(feed, token)

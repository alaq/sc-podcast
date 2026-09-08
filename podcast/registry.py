"""Automatic source registration, shared work queue and bounded refresh ticks."""

import hashlib
import json
import os
import secrets
import time
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import requests
from qstash import QStash

from podcast.config import normalize_feed
from podcast.soundcloud import SourceError
from podcast.store import RELEASE, StorageError, compact
from podcast.sync import Synchronizer


class CapacityError(ValueError):
    pass


def source_feed(value, session=requests):
    value = str(value).strip()
    if len(value) > 2048:
        raise ValueError("The SoundCloud URL is too long.")
    if value.startswith(("soundcloud.com/", "www.soundcloud.com/", "on.soundcloud.com/", "m.soundcloud.com/")):
        value = "https://" + value
    if "://" in value:
        for _ in range(4):
            parsed = urlsplit(value)
            if parsed.scheme not in ("https", "http") or parsed.username or parsed.password or parsed.port:
                raise ValueError("Enter a public SoundCloud page URL.")
            if parsed.hostname in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
                if "secret_token" in parsed.query:
                    raise ValueError("Private SoundCloud links are not supported.")
                value = parsed.path
                break
            if parsed.scheme != "https" or parsed.hostname != "on.soundcloud.com":
                raise ValueError("Enter a public SoundCloud page URL.")
            try:
                with session.get(value, allow_redirects=False, stream=True, timeout=(2, 3)) as response:
                    if response.status_code not in (301, 302, 303, 307, 308):
                        raise ValueError("The SoundCloud share link could not be resolved.")
                    value = urljoin(value, response.headers.get("Location", ""))
            except requests.RequestException as exc:
                raise ValueError("The SoundCloud share link could not be resolved.") from exc
        else:
            raise ValueError("The SoundCloud share link has too many redirects.")
    if not value.strip("/"):
        raise ValueError("Enter a profile, Likes, reposts, playlist or track URL.")
    feed = normalize_feed(value)
    if feed.split("/")[0] in {"search", "discover", "stream", "you", "upload", "settings", "charts", "stations", "api", "about", "status", "add", "track"}:
        raise ValueError("Use a public profile, Likes, reposts, playlist or track page.")
    return feed


REGISTER = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
if ARGV[5] == '1' and redis.call('ZCARD', KEYS[4]) >= tonumber(ARGV[4]) then return -1 end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[2], ARGV[3])
if ARGV[5] == '1' then redis.call('ZADD', KEYS[4], ARGV[2], ARGV[3]) end
return 1
"""
RATE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]) or 3600) end
return n
"""


class Registry:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.prefix = config.namespace + ":registry:"

    def get(self, feed):
        return self.store.get_json(self.store.key(feed, "registration"))

    def register(self, feed, client=None, now=None):
        now = int(time.time()) if now is None else now
        existing = self.get(feed)
        if existing:
            self.touch(feed, now)
            return existing
        automatic = feed not in self.config.feeds
        if automatic and client:
            key = self.prefix + "rate:" + hashlib.sha256(client.encode()).hexdigest()
            if self.store.command("EVAL", RATE, 1, key) > 5:
                raise CapacityError("Too many new feeds. Please try again in an hour.")
        record = {"feed": feed, "created_at": now, "automatic": automatic}
        result = self.store.command("EVAL", REGISTER, 4, self.store.key(feed, "registration"),
                                    self.prefix + "due", self.prefix + "active", self.prefix + "automatic",
                                    compact(record), now, feed, self.config.max_auto_feeds, "1" if automatic else "0")
        if result == -1:
            raise CapacityError("This service is at its feed limit. Existing feeds still work.")
        return self.get(feed) if result == 0 else record

    def touch(self, feed, now=None):
        now = int(time.time()) if now is None else now
        key = self.store.key(feed, "activity-touch")
        if self.store.command("SET", key, "1", "NX", "EX", 3600) == "OK":
            self.store.command("ZADD", self.prefix + "active", now, feed)
            # Wake an idle feed without adding a separate recurring schedule.
            self.store.command("ZADD", self.prefix + "due", "LT", now, feed)

    def due(self, now):
        return self.store.command("ZRANGEBYSCORE", self.prefix + "due", "-inf", now, "LIMIT", 0, self.config.max_auto_feeds + len(self.config.feeds))

    def later(self, feed, when):
        self.store.command("ZADD", self.prefix + "due", int(when), feed)


def queue_work(config, store, feed, batches=25, client=None):
    """One lease per feed prevents concurrent subscribers creating duplicate chains."""
    if not config.queue_enabled or not config.qstash_token:
        return False
    key = store.key(feed, "queued-work")
    token = secrets.token_hex(16)
    if store.command("SET", key, token, "NX", "EX", 180) != "OK":
        return False
    try:
        budget_key = config.namespace + ":bootstrap-budget:" + str(int(time.time()) // 86400)
        count = store.command("EVAL", RATE, 1, budget_key, 90000)
        if count > 500:
            store.command("EVAL", RELEASE, 1, key, token)
            return False  # Remaining preparation progresses in the shared tick.
        client = client or QStash(config.qstash_token, retry=False, base_url=os.environ.get("QSTASH_URL") or None)
        client.message.publish_json(url=config.sync_url, body={"feed": feed, "bootstrap": batches, "job_token": token},
                                    retries=2, timeout="60s", delay="2s")
        return True
    except Exception:
        store.command("EVAL", RELEASE, 1, key, token)
        return False  # The shared tick will recover work if delivery is unavailable.


def continue_work(config, store, feed, batches, token):
    if not token or store.command("EVAL", RELEASE, 1, store.key(feed, "queued-work"), token) != 1:
        return
    status = store.status(feed)
    due = status.get("retry_at", 0)
    if batches > 1 and (status.get("has_more") or (due and due <= time.time())):
        queue_work(config, store, feed, batches - 1)


def run_tick(config, store, source_factory, clock=time.time, monotonic=time.monotonic):
    """One recurring QStash delivery refreshes due feeds within a shared budget."""
    token = store.acquire("@shared-tick")
    if not token:
        return {"state": "already_running"}
    registry = Registry(config, store)
    now = int(clock())
    deadline = monotonic() + config.budget_seconds
    source = None
    results = []
    try:
        for feed in config.feeds:
            if not registry.get(feed):
                registry.register(feed, now=now)
        due = registry.due(now)
        # Existing configured feeds keep their five-minute refresh and migration gate.
        due = [f for f in config.feeds if f in due] + [f for f in due if f not in config.feeds]
        for feed in due:
            remaining = int(deadline - monotonic())
            if remaining < 15:
                break
            record = registry.get(feed)
            if not record:
                continue
            last_seen = float(store.command("ZSCORE", registry.prefix + "active", feed) or 0)
            if record["automatic"] and last_seen < now - 30 * 86400:
                registry.later(feed, now + 86400)
                continue
            try:
                source = source or source_factory()
                result = Synchronizer(replace(config, budget_seconds=remaining), store, source, clock=clock, monotonic=monotonic).run(feed, activate=record["automatic"])
                results.append({"feed": feed, "state": result["state"]})
                status = store.status(feed)
                retry = status.get("retry_at", 0)
                delay = 300 if not record["automatic"] or status.get("has_more") or (retry and retry <= now) else config.auto_refresh_seconds
                registry.later(feed, now + delay)
            except (SourceError, StorageError):
                results.append({"feed": feed, "state": "retrying"})
                registry.later(feed, now + 300)
        return {"state": "checked", "feeds": results}
    finally:
        if source:
            source.close()
        store.release("@shared-tick", token)

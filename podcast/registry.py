"""Automatic registration, request-driven work and the main feed's refresh tick."""

import hashlib
import os
import secrets
import time
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import requests

from podcast.config import DEFAULT_FEED, normalize_feed
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
if ARGV[5] == '1' and redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[4]) then return -1 end
redis.call('SET', KEYS[1], ARGV[1])
if ARGV[6] == '1' then redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3]) end
if ARGV[5] == '1' then redis.call('ZADD', KEYS[3], ARGV[2], ARGV[3]) end
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
            return existing
        automatic = feed not in self.config.feeds
        if automatic and client:
            key = self.prefix + "rate:" + hashlib.sha256(client.encode()).hexdigest()
            if self.store.command("EVAL", RATE, 1, key) > 5:
                raise CapacityError("Too many new feeds. Please try again in an hour.")
        record = {"feed": feed, "created_at": now, "automatic": automatic}
        result = self.store.command("EVAL", REGISTER, 3, self.store.key(feed, "registration"),
                                    self.prefix + "due", self.prefix + "automatic",
                                    compact(record), now, feed, self.config.max_auto_feeds, "1" if automatic else "0",
                                    "1" if feed == DEFAULT_FEED else "0")
        if result == -1:
            raise CapacityError("This service is at its feed limit. Existing feeds still work.")
        return self.get(feed) if result == 0 else record

    def later(self, feed, when):
        self.store.command("ZADD", self.prefix + "due", int(when), feed)


def request_work(config, store, feed, status=None):
    """Serve saved RSS regardless of enqueue failure; only demand starts work."""
    if feed == DEFAULT_FEED or not config.queue_enabled or not config.qstash_token:
        return False
    try:
        now = time.time()
        status = store.status(feed) if status is None else status
        retry = status.get("retry_at", 0)
        pending = status.get("has_more") or (retry and retry <= now)
        stale = not status.get("last_success_at") or now - status["last_success_at"] >= config.auto_refresh_seconds
        if not pending and not stale and not status.get("error"):
            return False
        # A failed source or delivery must not turn five-second progress polling
        # into repeated jobs. Keep the cooldown even when enqueueing fails.
        if now - status.get("last_checked_at", 0) < 60:
            return False
        if store.command("SET", store.key(feed, "requested-work"), "1", "NX", "EX", 60) != "OK":
            return False
        return queue_work(config, store, feed)
    except StorageError:
        return False


def queue_work(config, store, feed, batches=25, session=requests):
    """One lease per feed prevents concurrent subscribers creating duplicate chains."""
    if not config.queue_enabled or not config.qstash_token:
        return False
    key = store.key(feed, "queued-work")
    token = secrets.token_hex(16)
    if store.command("SET", key, token, "NX", "EX", 180) != "OK":
        return False
    try:
        # Retain the existing key across rollout; now covers all on-demand work.
        budget_key = config.namespace + ":bootstrap-budget:" + str(int(time.time()) // 86400)
        count = store.command("EVAL", RATE, 1, budget_key, 90000)
        if count > 500:
            store.command("EVAL", RELEASE, 1, key, token)
            return False  # A later client request can retry after the daily reset.
        # The SDK has a ten-minute read timeout. Enqueueing on an RSS request
        # instead needs a short timeout, independent of the job's 60s timeout.
        endpoint = (os.environ.get("QSTASH_URL") or "https://qstash.upstash.io").rstrip("/")
        with session.post(endpoint + "/v2/publish/" + config.sync_url,
                          headers={"Authorization": "Bearer " + config.qstash_token, "Content-Type": "application/json",
                                   "Upstash-Method": "POST", "Upstash-Retries": "2", "Upstash-Timeout": "60s", "Upstash-Delay": "2s"},
                          json={"feed": feed, "bootstrap": batches, "job_token": token},
                          timeout=(0.5, 1.5), allow_redirects=False) as response:
            if not 200 <= response.status_code < 300:
                raise requests.HTTPError("Enqueue failed")
        return True
    except Exception:
        store.command("EVAL", RELEASE, 1, key, token)
        return False  # A later client request recovers unavailable delivery.


def continue_work(config, store, feed, batches, token):
    if not token or store.command("EVAL", RELEASE, 1, store.key(feed, "queued-work"), token) != 1:
        return
    status = store.status(feed)
    due = status.get("retry_at", 0)
    if batches > 1 and (status.get("has_more") or (due and due <= time.time())):
        queue_work(config, store, feed, batches - 1)


def run_tick(config, store, source_factory, clock=time.time, monotonic=time.monotonic):
    """Only the main feed gets recurring work, including after old-index migration."""
    token = store.acquire("@shared-tick")
    if not token:
        return {"state": "already_running"}
    registry = Registry(config, store)
    now = int(clock())
    deadline = monotonic() + config.budget_seconds
    source = None
    results = []
    try:
        for feed in (DEFAULT_FEED,) if DEFAULT_FEED in config.feeds else ():
            if not registry.get(feed):
                registry.register(feed, now=now)
            due_at = float(store.command("ZSCORE", registry.prefix + "due", feed) or 0)
            if due_at > now:
                continue
            remaining = int(deadline - monotonic())
            if remaining < 15:
                break
            try:
                source = source or source_factory()
                result = Synchronizer(replace(config, budget_seconds=remaining), store, source, clock=clock, monotonic=monotonic).run(feed)
                results.append({"feed": feed, "state": result["state"]})
                registry.later(feed, now + 300)
            except (SourceError, StorageError):
                results.append({"feed": feed, "state": "retrying"})
                registry.later(feed, now + 300)
        return {"state": "checked", "feeds": results}
    finally:
        if source:
            source.close()
        store.release("@shared-tick", token)

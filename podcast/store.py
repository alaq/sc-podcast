"""Upstash REST storage with atomic publication and fenced synchronization."""

import base64
import gzip
import hashlib
import json
import secrets

import requests


class StorageError(RuntimeError):
    pass


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pack(value):
    return base64.b64encode(gzip.compress(compact(value).encode(), mtime=0)).decode()


def unpack(value):
    return json.loads(gzip.decompress(base64.b64decode(value))) if value else None


PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
for i = 2, #KEYS do redis.call('SET', KEYS[i], ARGV[i]) end
return 1
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class Store:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()

    def command(self, *args):
        if not self.config.redis_url or not self.config.redis_token:
            raise StorageError("Redis is not configured")
        try:
            response = self.session.post(
                self.config.redis_url.rstrip("/"), json=list(args),
                headers={"Authorization": "Bearer " + self.config.redis_token}, timeout=(3, 5),
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise StorageError("Redis rejected a command")
            return data["result"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            # Do not include URLs, response bodies, or credentials in public logs.
            raise StorageError("Redis request failed") from exc

    def key(self, feed, suffix):
        ident = hashlib.sha256(feed.encode()).hexdigest()[:24]
        return f"{self.config.namespace}:feed:{ident}:{suffix}"

    def get_json(self, key):
        raw = self.command("GET", key)
        try:
            return json.loads(raw) if raw else None
        except (TypeError, ValueError) as exc:
            raise StorageError("Invalid stored JSON") from exc

    def status(self, feed):
        return self.get_json(self.key(feed, "status")) or {}

    def state(self, feed):
        try:
            return unpack(self.command("GET", self.key(feed, "state"))) or {}
        except (ValueError, OSError) as exc:
            raise StorageError("Invalid stored feed state") from exc

    def manifest(self, feed):
        return self.get_json(self.key(feed, "manifest"))

    def body(self, key):
        raw = self.command("GET", key)
        if not raw:
            raise StorageError("Snapshot body is unavailable")
        try:
            return gzip.decompress(base64.b64decode(raw))
        except (ValueError, OSError) as exc:
            raise StorageError("Invalid snapshot body") from exc

    def acquire(self, feed):
        token = secrets.token_hex(24)
        return token if self.command("SET", self.key(feed, "lock"), token, "NX", "EX", 120) == "OK" else None

    def release(self, feed, token):
        return self.command("EVAL", RELEASE, 1, self.key(feed, "lock"), token)

    def commit(self, feed, token, status, state=None, manifest=None, bodies=None, tracks=()):
        values = {self.key(feed, "status"): compact(status)}
        if state is not None:
            values[self.key(feed, "state")] = pack(state)
        if manifest is not None:
            values[self.key(feed, "manifest")] = compact(manifest)
        values.update(bodies or {})
        for track in tracks:
            values[self.track_key(track["enclosure_path"])] = compact({k: track.get(k) for k in ("id", "webpage_url", "progressive_url", "length")})
        keys = [self.key(feed, "lock"), *values]
        if self.command("EVAL", PUBLISH, len(keys), *keys, token, *values.values()) != 1:
            raise StorageError("Sync lock expired; publication cancelled")

    def legacy_dates(self, feed, ids):
        if not ids:
            return {}
        values = self.command("MGET", *(f"feed:{feed}:track:{i}" for i in ids))
        return dict(zip(ids, values))

    def audio(self, track_path):
        return self.get_json(f"{self.config.namespace}:audio:{hashlib.sha256(track_path.encode()).hexdigest()}")

    def track_key(self, track_path):
        return f"{self.config.namespace}:track-path:{hashlib.sha256(track_path.encode()).hexdigest()}"

    def track(self, track_path):
        return self.get_json(self.track_key(track_path))

    def cache_audio(self, track_path, info, ttl):
        if ttl > 0:
            self.command("SET", f"{self.config.namespace}:audio:{hashlib.sha256(track_path.encode()).hexdigest()}", compact(info), "EX", ttl)

    def expire_old_bodies(self, keys):
        for key in keys:
            self.command("EXPIRE", key, 86400)

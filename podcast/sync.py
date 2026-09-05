"""Bounded synchronization with durable discovery and atomic RSS publication."""

import base64
import gzip
import hashlib
import time

from podcast.feed import render
from podcast.soundcloud import SourceError, epoch
from podcast.store import StorageError, compact


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def ready_entries(state, limit):
    return sorted((x for x in state.get("tracks", {}).values() if x.get("length")),
                  key=lambda x: (x.get("liked_at", 0), x["id"]), reverse=True)[:limit]


def migration_entries(state, limit=None):
    """All discovered history, including unavailable sets that may recover later."""
    cutoff = state.get("bootstrap_at", 0)
    return [x for x in state.get("tracks", {}).values() if x.get("liked_at", cutoff + 1) <= cutoff and not x.get("legacy_identity")]


def build_snapshot(store, config, feed, state, previous, now):
    entries = ready_entries(state, config.max_items)
    if not entries:
        return None, {}, []
    variants = {}
    bodies = {}
    for base in config.bases:
        body = render(feed, entries, base, state.get("title"))
        checksum = hashlib.sha256(body).hexdigest()
        key = store.key(feed, "body:" + checksum)
        variants[base] = {"key": key, "etag": f'"{checksum}"', "length": len(body)}
        bodies[key] = base64.b64encode(gzip.compress(body, mtime=0)).decode()
    if previous and previous.get("variants") == variants:
        return None, {}, []
    manifest = {"variants": variants, "modified_at": now, "count": len(entries),
                "previous": (previous or {}).get("variants", {})}
    keep = {v["key"] for group in (variants, manifest["previous"]) for v in group.values()}
    retire = [v["key"] for v in (previous or {}).get("previous", {}).values() if v["key"] not in keep]
    return manifest, bodies, retire


class Synchronizer:
    def __init__(self, config, store, source, clock=time.time, monotonic=time.monotonic):
        self.config, self.store, self.source = config, store, source
        self.clock, self.monotonic = clock, monotonic

    def run(self, feed, publish=True):
        token = self.store.acquire(feed)
        if not token:
            return {"state": "already_running", "feed": feed}
        status = {}
        deadline = self.monotonic() + self.config.budget_seconds
        now = int(self.clock())
        try:
            status = self.store.status(feed)
            head = self.source.listing(feed)
            signature = digest(head["entries"])
            retry_due = status.get("retry_at", 0) and status["retry_at"] <= now
            if signature == status.get("head_signature") and not retry_due and not status.get("has_more"):
                status.update(last_checked_at=now, error=None)
                self.store.commit(feed, token, status)
                return {"state": "unchanged", "feed": feed, "count": status.get("published_count", 0)}

            state = self.store.state(feed)
            if not state:
                state = {"tracks": {}, "bootstrap_at": now, "rollout_ready": False, "title": head["title"],
                         "pending_pages": []}
            if not state.get("backfill_initialized"):
                state.update(backfill_cursor=head["next"], backfill_initialized=True)
            known = set(state["tracks"])
            fresh = list(head["entries"])
            pending = state.setdefault("pending_pages", [])
            if known and fresh and not any(x["id"] in known for x in fresh) and head["next"] and head["next"] not in pending:
                pending.append(head["next"])

            # At most one additional page per invocation; cursor progress persists.
            cursor = pending.pop(0) if pending else None
            candidates = [x for x in state["tracks"].values() if x.get("length") or not x.get("attempts")]
            if not cursor and len(candidates) < self.config.max_items and state.get("backfill_cursor"):
                cursor = state["backfill_cursor"]
            if cursor and self.monotonic() < deadline - 10:
                page = self.source.listing(feed, cursor)
                fresh.extend(page["entries"])
                if cursor == state.get("backfill_cursor"):
                    state["backfill_cursor"] = page["next"]
                elif page["next"] and not any(x["id"] in known for x in page["entries"]):
                    pending.append(page["next"])
            elif cursor and cursor != state.get("backfill_cursor"):
                pending.insert(0, cursor)

            new_ids = list(dict.fromkeys(x["id"] for x in fresh if x["id"] not in known))
            legacy_dates = self.store.legacy_dates(feed, new_ids)
            changed = []
            for entry in fresh:
                old = state["tracks"].get(entry["id"], {})
                seed = state.get("legacy_seeds", {}).get(entry["enclosure_path"], {})
                merged = {**old, **entry}
                for key in ("enclosure_path", "published_at", "legacy_identity", "guid_by_base"):
                    if key in old:
                        merged[key] = old[key]
                    elif key in seed:
                        merged[key] = seed[key]
                if not merged.get("published_at"):
                    merged["published_at"] = epoch(legacy_dates.get(entry["id"])) or entry.get("liked_at") or now
                if old != merged:
                    state["tracks"][entry["id"]] = merged
                    changed.append(merged)
            state["title"] = head["title"]
            state["skipped_source_items"] = head.get("skipped", 0)
            status.update(head_signature=signature, last_checked_at=now, error=None)

            # Save discovery before any slow preparation. A timeout cannot lose Likes.
            status.update(retry_at=now, has_more=True)
            self.store.commit(feed, token, status, state=state, tracks=changed)
            prepared = []
            ordered = sorted(state["tracks"].values(), key=lambda x: (x.get("liked_at", 0), x["id"]), reverse=True)
            attempts = 0
            for track in ordered:
                if attempts >= self.config.max_audio or self.monotonic() >= deadline - 12:
                    break
                if track.get("length") or track.get("retry_at", 0) > now:
                    continue
                # Retain all discovered metadata, but prepare only the useful window.
                ready = ready_entries(state, self.config.max_items)
                if len(ready) >= self.config.max_items and (track.get("liked_at", 0), track["id"]) < (ready[-1].get("liked_at", 0), ready[-1]["id"]):
                    continue
                attempts += 1
                try:
                    audio = self.source.resolve_audio(track)
                    track.update(length=audio["length"], audio_type="audio/mpeg", prepared_at=now, retry_at=0, error=None)
                    prepared.append(track)
                    ttl = max(0, min(120, audio.get("expires_at", 0) - now - 30))
                    try:
                        self.store.cache_audio(track["enclosure_path"], audio, ttl)
                    except StorageError:
                        pass  # Playback cache is expendable; feed state is not.
                except SourceError:
                    count = track.get("attempts", 0) + 1
                    track.update(attempts=count, retry_at=now + min(21600, 900 * 2 ** min(count - 1, 5)), error="audio_unavailable")
            remaining = [x for x in ordered if not x.get("length")]
            ready = ready_entries(state, self.config.max_items)
            eligible = remaining if len(ready) < self.config.max_items else [x for x in remaining if (x.get("liked_at", 0), x["id"]) > (ready[-1].get("liked_at", 0), ready[-1]["id"])]
            retry_at = min((x.get("retry_at") or now for x in eligible), default=0)
            status.update(retry_at=retry_at, has_more=bool(pending or (len(ready) + sum(not x.get("attempts") for x in remaining) < self.config.max_items and state.get("backfill_cursor"))),
                          discovered_count=len(state["tracks"]), ready_count=len(ready), unavailable_count=sum(bool(x.get("attempts")) for x in eligible),
                          rollout_ready=state.get("rollout_ready", False), last_success_at=now)
            manifest, bodies, retired = None, {}, []
            if publish and state.get("rollout_ready"):
                manifest, bodies, retired = build_snapshot(self.store, self.config, feed, state, self.store.manifest(feed), now)
                if manifest:
                    status.update(published_count=manifest["count"], last_published_at=now)
            self.store.commit(feed, token, status, state=state, manifest=manifest, bodies=bodies, tracks=prepared)
            if retired:
                try:
                    self.store.expire_old_bodies(retired)
                except StorageError:
                    pass  # Cleanup failure cannot invalidate a successful publish.
            return {"state": "published" if manifest else "prepared", "feed": feed, "discovered": len(state["tracks"]),
                    "ready": len(ready), "published": status.get("published_count", 0), "unavailable": status["unavailable_count"]}
        except (SourceError, StorageError):
            if status:
                try:
                    status.update(last_checked_at=now, error="sync_failed")
                    self.store.commit(feed, token, status)
                except StorageError:
                    pass
            raise
        finally:
            try:
                self.store.release(feed, token)
            except StorageError:
                pass

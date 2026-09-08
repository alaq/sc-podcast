"""Maintenance commands; credentials are read from the environment or .env."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from qstash import QStash

from podcast.config import Config, DEFAULT_FEED, normalize_feed
from podcast.feed import render
from podcast.migrate import publish, seed
from podcast.soundcloud import SoundCloud
from podcast.store import Store
from podcast.sync import Synchronizer, migration_entries, ready_entries
from podcast.registry import Registry, run_tick


def schedule_id(config, feed):
    return "sc-podcast-" + hashlib.sha256((config.namespace + ":" + feed).encode()).hexdigest()[:20]


def schedule(config, store, feed, action, client=None, shared=False):
    if not config.qstash_token or not config.signing_key or not config.next_signing_key:
        raise ValueError("Configure QStash token and both signing keys first")
    client = client or QStash(config.qstash_token, retry=False, base_url=os.environ.get("QSTASH_URL") or None)
    ident = schedule_id(config, feed)
    if action == "create":
        if not shared and (not store.manifest(feed) or not store.state(feed).get("rollout_ready")):
            raise ValueError("Publish and verify the initial snapshot before enabling its schedule")
        client.schedule.create(destination=config.sync_url, cron="*/5 * * * *", body=json.dumps({"tick": True} if shared else {"feed": feed}),
                               content_type="application/json", method="POST", retries=2, timeout="60s", schedule_id=ident)
    elif action == "pause":
        client.schedule.pause(ident)
    value = client.schedule.get(ident)
    return {"schedule_id": ident, "destination": value.destination, "cron": value.cron, "paused": value.paused}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", default=DEFAULT_FEED)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="Preserve dates and IDs from the existing live feed")
    sub.add_parser("status")
    sub.add_parser("tick", help="Run one bounded shared refresh tick")
    sync = sub.add_parser("sync", help="Prepare metadata/audio in bounded batches")
    sync.add_argument("--runs", type=int, default=1)
    export = sub.add_parser("export", help="Export candidate RSS and historical-announcement identities")
    export.add_argument("--directory", type=Path, default=Path("migration-data"))
    pub = sub.add_parser("publish", help="Baseline historical announcements, then atomically expose prepared RSS")
    group = pub.add_mutually_exclusive_group(required=True)
    group.add_argument("--announcement-db", type=Path)
    group.add_argument("--no-announcement-consumer", action="store_true")
    pub.add_argument("--backup-dir", type=Path, default=Path("migration-data"))
    pub.add_argument("--minimum-items", type=int, help="Explicitly accept fewer than FEED_MAX_ITEMS")
    sched = sub.add_parser("schedule")
    sched.add_argument("action", choices=("create", "status", "pause"))
    sched.add_argument("--shared", action="store_true", help="Use this existing schedule for all automatically registered sources")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        feed = normalize_feed(args.feed)
        if feed not in config.feeds and args.command not in ("sync", "status", "export"):
            raise ValueError("Feed must be listed in SC_FEEDS")
        store = Store(config)
        if args.command == "tick":
            result = run_tick(config, store, SoundCloud)
        elif args.command == "seed":
            result = seed(config, store, feed)
        elif args.command == "status":
            result = {"namespace": config.namespace, "feed": feed, **store.status(feed)}
        elif args.command == "sync":
            registration = Registry(config, store).get(feed)
            if feed not in config.feeds and not registration:
                raise ValueError("Add this source before synchronizing it")
            source = SoundCloud()
            try:
                for _ in range(max(1, min(args.runs, 100))):
                    result = Synchronizer(config, store, source).run(feed, activate=bool(registration and registration["automatic"]))
                    print(json.dumps(result), flush=True)
                    if result["state"] in ("unchanged", "already_running"):
                        break
            finally:
                source.close()
            return 0
        elif args.command == "export":
            state = store.state(feed)
            entries = ready_entries(state, config.max_items)
            args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for i, base in enumerate(config.bases):
                (args.directory / f"candidate-{i}.xml").write_bytes(render(feed, entries, base, state.get("title"), state.get("image")))
            historical = [{k: x.get(k) for k in ("id", "enclosure_path", "title", "liked_at", "published_at")} for x in migration_entries(state, config.max_items)]
            (args.directory / "historical-announcements.json").write_text(json.dumps(historical, indent=2))
            result = {"episodes": len(entries), "historical": len(historical), "directory": str(args.directory)}
        elif args.command == "publish":
            result = publish(config, store, feed, args.announcement_db, args.backup_dir, args.no_announcement_consumer, args.minimum_items)
        else:
            result = schedule(config, store, feed, args.action, shared=args.shared)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Third-party exceptions may contain signed URLs/tokens; keep them off stdout.
        print(f"Operation failed ({type(exc).__name__}). Check configuration, status and the migration runbook.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

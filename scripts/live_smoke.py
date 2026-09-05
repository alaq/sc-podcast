"""Read-only live source smoke test with an isolated, in-memory Redis emulator.

Requires requirements-dev.txt. Writes candidate RSS and timings locally; never
connects to real Redis, QStash, or the announcement database.
"""

import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from conftest import MemoryStore
from podcast.config import Config, DEFAULT_FEED
from podcast.feed import render
from podcast.migrate import publish, seed
from podcast.soundcloud import SoundCloud
from podcast.store import pack
from podcast.sync import Synchronizer, ready_entries


def main():
    config = Config(namespace="local-smoke-only")  # deliberately ignores service credentials
    store = MemoryStore(config)
    start = time.monotonic()
    seed_result = seed(config, store, DEFAULT_FEED)
    source = SoundCloud()
    batches = []
    try:
        for i in range(40):
            before = time.monotonic()
            result = Synchronizer(config, store, source).run(DEFAULT_FEED)
            batches.append({**result, "seconds": round(time.monotonic() - before, 3)})
            print(json.dumps({"batch": i + 1, **batches[-1]}), flush=True)
            if result.get("ready", 0) >= config.max_items or result["state"] == "unchanged":
                break
    finally:
        source.close()
    state = store.state(DEFAULT_FEED)
    entries = ready_entries(state, config.max_items)
    if len(entries) < config.max_items:
        raise RuntimeError("Live probe has not prepared 200 playable episodes")
    publish(config, store, DEFAULT_FEED, no_consumer=True)
    directory = ROOT / "migration-data" / "live-smoke"
    directory.mkdir(parents=True, exist_ok=True)
    for i, base in enumerate(config.bases):
        body = render(DEFAULT_FEED, entries, base, state["title"])
        assert len(ET.fromstring(body).findall("./channel/item")) == 200
        (directory / f"candidate-{i}.xml").write_bytes(body)
    # Kept locally for debugging. No signed audio-download URLs are included.
    (directory / "state.gz.b64").write_text(pack(state))
    measurements = []
    for _ in range(50):
        before = time.monotonic()
        body = store.body(store.manifest(DEFAULT_FEED)["variants"][config.bases[0]]["key"])
        measurements.append((time.monotonic() - before) * 1000)
    output = {"seed": seed_result, "episodes": len(entries), "discovered": len(state["tracks"]),
              "rss_bytes": len(body), "compressed_state_bytes": len(pack(state)), "batches": batches,
              "seconds_total": round(time.monotonic() - start, 3),
              "local_saved_feed_median_ms": round(sorted(measurements)[len(measurements)//2], 3),
              "caveat": "In-memory Redis timings exclude hosted Redis, network, Vercel startup, and CDN latency."}
    (directory / "measurements.json").write_text(json.dumps(output, indent=2))
    print(json.dumps({k: v for k, v in output.items() if k != "batches"}), flush=True)


if __name__ == "__main__":
    main()

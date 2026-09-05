import copy
from dataclasses import replace

import fakeredis
import pytest

from podcast.config import Config, DEFAULT_FEED
from podcast.soundcloud import SourceError
from podcast.store import Store


class MemoryStore(Store):
    """Real Redis commands and Lua executed in fakeredis, no HTTP mock semantics."""
    def __init__(self, config):
        super().__init__(config)
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.calls = []

    def command(self, *args):
        self.calls.append(args)
        value = self.redis.execute_command(*args)
        return "OK" if args[0] == "SET" and value is True else value


def track(n, **changes):
    return {"id": str(n), "title": f"Set {n}", "uploader": "DJ", "duration": 3600,
            "description": "A & B <live> \x01 🎧", "webpage_url": f"https://soundcloud.com/dj/set-{n}",
            "enclosure_path": f"dj/set-{n}", "artwork": "https://i1.sndcdn.com/art.jpg",
            "original_published_at": 1600000000 + n, "liked_at": 1700000000 + n,
            "progressive_url": f"https://api-v2.soundcloud.com/media/{n}", **changes}


class Source:
    def __init__(self, entries=None, pages=None):
        self.pages = pages or {None: {"entries": entries if entries is not None else [track(n) for n in range(1, 201)], "next": None, "title": "kado", "skipped": 0}}
        self.bad = set()
        self.audio_calls = []
        self.list_calls = []

    def listing(self, feed, cursor=None):
        self.list_calls.append(cursor)
        value = self.pages[cursor]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def resolve_audio(self, entry):
        self.audio_calls.append(entry["id"])
        if entry["id"] in self.bad:
            raise SourceError("Unavailable")
        return {"url": "https://cf-media.sndcdn.com/audio.mp3?Expires=2000000000", "length": 100000000 + int(entry["id"]), "expires_at": 2000000000, "type": "audio/mpeg"}

    def close(self):
        pass


@pytest.fixture
def config():
    return replace(Config(), sync_secret="test-sync-secret", max_audio=8)


@pytest.fixture
def store(config):
    return MemoryStore(config)


@pytest.fixture
def source():
    return Source()


@pytest.fixture
def feed():
    return DEFAULT_FEED

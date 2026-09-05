"""Small, explicit configuration shared by the server and maintenance CLI."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_FEED = "kado-nyc/likes"


def load_env():
    path = Path(__file__).resolve().parent.parent / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def normalize_feed(path):
    path = unquote(urlsplit(path).path).strip("/") or DEFAULT_FEED
    parts = path.split("/")
    if any(not p or p in (".", "..") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in p) for p in parts):
        raise ValueError("Invalid SoundCloud path")
    if len(parts) == 1:
        parts.append("tracks")
    if len(parts) > 3 or (len(parts) == 3 and parts[1] != "sets"):
        raise ValueError("Invalid SoundCloud path")
    return "/".join(parts).lower()


@dataclass(frozen=True)
class Config:
    redis_url: str = ""
    redis_token: str = ""
    namespace: str = "sc-podcast:v2"
    feeds: tuple = (DEFAULT_FEED,)
    bases: tuple = ("https://sc-podcast.vercel.app", "https://podcast.alaq.io")
    max_items: int = 200
    sync_secret: str = ""
    signing_key: str = ""
    next_signing_key: str = ""
    sync_url: str = "https://sc-podcast.vercel.app/api/sync"
    qstash_token: str = ""
    max_audio: int = 8
    budget_seconds: int = 40
    ping_overcast: bool = False

    @classmethod
    def from_env(cls):
        load_env()
        namespace = os.environ.get("SC_PODCAST_NAMESPACE", "sc-podcast:v2")
        if os.environ.get("VERCEL_ENV") == "preview":
            branch = os.environ.get("VERCEL_GIT_COMMIT_REF") or os.environ.get("VERCEL_URL", "preview")
            namespace += ":preview:" + hashlib.sha256(branch.encode()).hexdigest()[:12]
        bases = tuple(x.strip().rstrip("/") for x in os.environ.get("PUBLIC_BASE_URLS", "https://sc-podcast.vercel.app,https://podcast.alaq.io").split(",") if x.strip())
        for base in bases:
            parsed = urlsplit(base)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("PUBLIC_BASE_URLS must contain plain HTTP(S) origins")
        if not bases:
            raise ValueError("At least one public base URL is required")
        feeds = tuple(dict.fromkeys(normalize_feed(x.strip()) for x in os.environ.get("SC_FEEDS", DEFAULT_FEED).split(",") if x.strip()))
        if not feeds:
            raise ValueError("At least one SoundCloud feed is required")
        return cls(
            redis_url=os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL", ""),
            redis_token=os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""),
            namespace=namespace,
            feeds=feeds,
            bases=bases,
            max_items=max(1, min(1000, int(os.environ.get("FEED_MAX_ITEMS", "200")))),
            sync_secret=os.environ.get("SYNC_SECRET", ""),
            signing_key=os.environ.get("QSTASH_CURRENT_SIGNING_KEY", ""),
            next_signing_key=os.environ.get("QSTASH_NEXT_SIGNING_KEY", ""),
            sync_url=os.environ.get("SYNC_URL", bases[0] + "/api/sync"),
            qstash_token=os.environ.get("QSTASH_TOKEN", ""),
            max_audio=max(1, min(30, int(os.environ.get("MAX_AUDIO_PREPARATIONS", "8")))),
            budget_seconds=max(5, min(45, int(os.environ.get("SYNC_BUDGET_SECONDS", "40")))),
            ping_overcast=os.environ.get("PING_OVERCAST") == "1" and os.environ.get("VERCEL_ENV") != "preview",
        )

    def base_for_host(self, host):
        return next((b for b in self.bases if urlsplit(b).netloc.lower() == host.lower()), self.bases[0])

"""HTTP delivery of persisted feeds and short audio redirects."""

import gzip
import hmac
import html
import json
import logging
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests
from qstash import Receiver

from podcast.config import Config, DEFAULT_FEED, normalize_feed
from podcast.soundcloud import SoundCloud, SourceError
from podcast.store import Store, StorageError
from podcast.sync import Synchronizer

LOG = logging.getLogger("sc-podcast")


def not_modified(headers, etag, modified):
    supplied = headers.get("If-None-Match")
    if supplied is not None:
        return any(x.strip().removeprefix("W/") in (etag, "*") for x in supplied.split(","))
    try:
        value = parsedate_to_datetime(headers.get("If-Modified-Since", ""))
        return value.tzinfo is not None and int(value.timestamp()) >= modified
    except (ValueError, TypeError, OverflowError):
        return False


def accepts_gzip(header):
    for part in header.lower().split(","):
        name, *params = part.strip().split(";")
        if name == "gzip":
            try:
                return all(float(p.strip()[2:]) > 0 for p in params if p.strip().startswith("q="))
            except ValueError:
                return False
    return False


class Handler(BaseHTTPRequestHandler):
    config_factory = staticmethod(Config.from_env)
    store_factory = staticmethod(Store)
    source_factory = staticmethod(SoundCloud)

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        supplied = headers or {}
        for name, value in supplied.items():
            self.send_header(name, str(value))
        if status != 304 and "Content-Length" not in supplied:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and status != 304:
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            config = self.config_factory()
            path = urlsplit(self.path).path
            if path == "/art.png":
                body = (Path(__file__).resolve().parent.parent / "api" / "art.png").read_bytes()
                self.reply(200, body, "image/png", {"Cache-Control": "public, max-age=86400"})
                return
            if path in ("/favicon.ico", "/api/sync"):
                self.reply(405 if path == "/api/sync" else 404, headers={"Cache-Control": "no-store"})
                return
            store = self.store_factory(config)
            base = config.base_for_host(self.headers.get("Host", ""))
            if path.startswith("/track/"):
                self.audio(config, store, unquote(path[len("/track/"):]))
                return
            if path in ("/about", "/status"):
                self.about(config, store, base, path == "/status")
                return
            feed = normalize_feed(path)
            if feed not in config.feeds:
                self.reply(404, b"This source has not been configured for synchronization.", headers={"Cache-Control": "no-store"})
                return
            manifest = store.manifest(feed)
            if not manifest:
                self.reply(503, b"The first feed snapshot is being prepared. Please try again later.", headers={"Retry-After": "300", "Cache-Control": "no-store"})
                return
            variant = manifest["variants"].get(base) or manifest["variants"][config.bases[0]]
            headers = {"Cache-Control": "public, max-age=0, must-revalidate", "Vercel-CDN-Cache-Control": "public, s-maxage=300, stale-while-revalidate=60",
                       "Last-Modified": format_datetime(datetime.fromtimestamp(manifest["modified_at"], timezone.utc), usegmt=True),
                       "ETag": "W/" + variant["etag"], "Vary": "Accept-Encoding"}
            # Validator first: unchanged refreshes never fetch the large Redis value.
            if not_modified(self.headers, variant["etag"], manifest["modified_at"]):
                self.reply(304, content_type="application/rss+xml; charset=utf-8", headers=headers)
                return
            if self.command == "HEAD" and not accepts_gzip(self.headers.get("Accept-Encoding", "")):
                headers["Content-Length"] = variant["length"]
                self.reply(200, content_type="application/rss+xml; charset=utf-8", headers=headers)
                return
            body = store.body(variant["key"])
            if accepts_gzip(self.headers.get("Accept-Encoding", "")):
                body = gzip.compress(body, mtime=0)
                headers["Content-Encoding"] = "gzip"
            self.reply(200, body, "application/rss+xml; charset=utf-8", headers)
        except ValueError:
            self.reply(400, b"Invalid request.", headers={"Cache-Control": "no-store"})
        except (StorageError, SourceError):
            self.reply(503, b"Temporarily unavailable. Please try again later.", headers={"Cache-Control": "no-store", "Retry-After": "60"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            LOG.warning("Request failed (%s)", type(exc).__name__)
            self.reply(503, b"Temporarily unavailable. Please try again later.", headers={"Cache-Control": "no-store", "Retry-After": "60"})

    def audio(self, config, store, track_path):
        parts = track_path.split("/")
        if len(parts) != 2 or any(not p or p in (".", "..") or any(c in p for c in "?#\\\r\n") for p in parts):
            self.reply(404, b"Unknown track.", headers={"Cache-Control": "no-store"})
            return
        now = int(time.time())
        try:
            audio = store.audio(track_path)
        except StorageError:
            audio = None
        if not audio or audio.get("expires_at", 0) <= now + 30:
            try:
                track = store.track(track_path)
            except StorageError:
                track = None
            track = track or {"webpage_url": "https://soundcloud.com/" + quote(track_path, safe="/")}
            source = self.source_factory()
            try:
                audio = source.resolve_audio(track)
            finally:
                source.close()
            ttl = max(0, min(120, audio.get("expires_at", 0) - now - 30))
            try:
                store.cache_audio(track_path, audio, ttl)
            except StorageError:
                pass
        self.reply(302, headers={"Location": audio["url"], "Cache-Control": "no-store"})

    def about(self, config, store, base, as_json):
        status = store.status(DEFAULT_FEED) if DEFAULT_FEED in config.feeds else store.status(config.feeds[0])
        public = {k: status.get(k) for k in ("last_checked_at", "last_success_at", "last_published_at", "published_count", "ready_count", "unavailable_count", "error")}
        if as_json:
            self.reply(200, json.dumps(public).encode(), "application/json", {"Cache-Control": "no-store"})
            return
        count = public.get("published_count") or 0
        checked = public.get("last_success_at")
        when = datetime.fromtimestamp(checked, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if checked else "Preparing the first feed"
        feed_url = base + "/"
        overcast = "overcast://x-callback-url/add?url=" + quote(feed_url, safe="")
        body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ACSv3 · SoundCloud sets</title><style>body{{font:18px/1.6 system-ui;margin:10vh auto;padding:24px;max-width:640px;color:#1d252b;background:#f6f3ee}}img{{width:140px;border-radius:18px}}a{{color:#a8420b}}code{{word-break:break-all}}.muted{{color:#59626a}}</style>
<img src="/art.png" alt="ACSv3 artwork"><h1>ACSv3</h1><p>DJ sets liked on SoundCloud, ready in your podcast app.</p>
<p><a href="{html.escape(overcast)}">Subscribe in Overcast</a> · <a href="{html.escape(feed_url)}">RSS feed</a></p>
<p>In Apple Podcasts, choose Follow a Show by URL and paste:</p><p><code>{html.escape(feed_url)}</code></p>
<p class="muted">{count} episodes available. Last successful sync: {when}.</p>
<p class="muted">{'A sync is being retried; the last saved feed remains available.' if public.get('error') else 'New Likes are checked every five minutes; your podcast app may refresh later.'}</p></html>'''
        self.reply(200, body.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/sync" or urlsplit(self.path).query:
            self.reply(404, headers={"Cache-Control": "no-store"})
            return
        try:
            config = self.config_factory()
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 4096:
                self.reply(413, headers={"Cache-Control": "no-store"})
                return
            raw = self.rfile.read(length).decode("utf-8")
            authorized = bool(config.sync_secret and hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + config.sync_secret))
            if not authorized and config.signing_key and config.next_signing_key:
                try:
                    Receiver(config.signing_key, config.next_signing_key).verify(signature=self.headers.get("Upstash-Signature", ""), body=raw, url=config.sync_url)
                    authorized = True
                except Exception:
                    pass
            if not authorized:
                self.reply(401, b"Unauthorized", headers={"Cache-Control": "no-store"})
                return
            data = json.loads(raw or "{}")
            feed = normalize_feed(data.get("feed", DEFAULT_FEED))
            if feed not in config.feeds:
                self.reply(400, b"Unknown configured feed", headers={"Cache-Control": "no-store"})
                return
            store = self.store_factory(config)
            source = self.source_factory()
            try:
                result = Synchronizer(config, store, source).run(feed)
            finally:
                source.close()
            if result["state"] == "published" and config.ping_overcast:
                for base in config.bases:
                    try:
                        requests.post("https://overcast.fm/ping", data={"urlprefix": base + ("/" if feed == DEFAULT_FEED else "/" + feed)}, timeout=(2, 3))
                    except requests.RequestException:
                        pass
            self.reply(200, json.dumps(result).encode(), "application/json", {"Cache-Control": "no-store"})
        except (ValueError, TypeError, UnicodeError, AttributeError):
            self.reply(400, b"Invalid sync request", headers={"Cache-Control": "no-store"})
        except Exception as exc:
            LOG.warning("Sync failed (%s)", type(exc).__name__)
            self.reply(503, b"Sync failed; previous feed preserved", headers={"Cache-Control": "no-store", "Retry-After": "60"})

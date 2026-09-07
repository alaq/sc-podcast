"""Keep rich listing metadata instead of extracting every track during RSS reads.

All access to yt-dlp's private SoundCloud API adapter is isolated here and covered
by fixture/smoke tests. The extractor dependency is deliberately pinned.
"""

import base64
import json
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests
import yt_dlp
from yt_dlp.extractor.soundcloud import SoundcloudUserIE

from podcast.artwork import episode_artwork


class SourceError(RuntimeError):
    pass


class QuietLogger:
    def debug(self, *_): pass
    def info(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


def epoch(value):
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        return epoch(value.get("value", value.get("result")))
    if isinstance(value, str):
        try:
            return int(float(value.strip('"')))
        except ValueError:
            try:
                parsed = json.loads(value)
                if parsed != value:
                    return epoch(parsed)
            except (ValueError, TypeError):
                pass
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    return None


def normalize_track(raw, liked_at=None):
    url = raw.get("permalink_url", "")
    parsed = urlsplit(url)
    if not raw.get("id") or parsed.hostname not in ("soundcloud.com", "www.soundcloud.com") or len(parsed.path.strip("/").split("/")) != 2:
        return None
    progressive = next((x for x in raw.get("media", {}).get("transcodings", [])
                        if x.get("format", {}).get("protocol") == "progressive"
                        and x.get("format", {}).get("mime_type", "").split(";")[0] == "audio/mpeg"
                        and not x.get("snipped") and "/preview/" not in x.get("url", "")), {})
    created = epoch(raw.get("created_at")) or 0
    artwork = raw.get("artwork_url") or raw.get("user", {}).get("avatar_url") or ""
    return {
        "id": str(raw["id"]), "title": raw.get("title") or "Untitled set",
        "uploader": raw.get("user", {}).get("username") or "Unknown artist",
        "duration": int((raw.get("duration") or 0) / 1000),
        "description": raw.get("description") or "",
        "webpage_url": urlunsplit(("https", "soundcloud.com", parsed.path, "", "")),
        "enclosure_path": parsed.path.strip("/"),
        "artwork": episode_artwork(artwork),
        "original_published_at": created, "liked_at": epoch(liked_at) or created,
        "progressive_url": progressive.get("url", ""),
    }


def clean_cursor(url):
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api-v2.soundcloud.com":
        raise SourceError("Unexpected SoundCloud pagination URL")
    query = parse_qs(parsed.query)
    query.pop("client_id", None)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))


def signed_url_expiry(url):
    query = parse_qs(urlsplit(url).query)
    for key in ("Expires", "expires"):
        if key in query:
            return epoch(query[key][0])
    try:
        value = query["Policy"][0].replace("-", "+").replace("_", "=").replace("~", "/")
        policy = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
        return int(policy["Statement"][0]["Condition"]["DateLessThan"]["AWS:EpochTime"])
    except (KeyError, ValueError, TypeError, IndexError):
        return None


class SoundCloud:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.ydl = yt_dlp.YoutubeDL({"quiet": True, "logger": QuietLogger(), "socket_timeout": 5,
                                   "retries": 0, "extractor_retries": 0, "cachedir": "/tmp/sc-podcast-yt-dlp"})
        self.ie = SoundcloudUserIE(self.ydl)
        try:
            self.ie.initialize()
        except Exception as exc:
            self.close()
            raise SourceError("SoundCloud initialization failed") from exc

    def close(self):
        self.ydl.close()
        self.session.close()

    def api(self, url, **kwargs):
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "api-v2.soundcloud.com":
            raise SourceError("Unexpected SoundCloud endpoint")
        try:
            return self.ie._call_api(url, "podcast", headers=self.ie._HEADERS, **kwargs)
        except Exception as exc:
            raise SourceError("SoundCloud request failed") from exc

    def listing(self, feed, cursor=None):
        """Return a single durable page; no audio-format resolution."""
        if cursor:
            data = self.api(clean_cursor(cursor))
            title = "SoundCloud"
        else:
            parts = feed.split("/")
            user_feed = len(parts) == 2 and parts[1] in ("likes", "reposts", "tracks")
            resolved = self.api("https://api-v2.soundcloud.com/resolve", query={"url": "https://soundcloud.com/" + (parts[0] if user_feed else feed)})
            title = resolved.get("username") or resolved.get("title") or parts[0]
            if user_feed:
                resource = f"users/{resolved['id']}/{parts[1]}"
                if parts[1] == "reposts":
                    resource = "stream/" + resource
                data = self.api("https://api-v2.soundcloud.com/" + resource, query={"limit": 200, "linked_partitioning": "1"})
            elif resolved.get("kind") == "track":
                data = {"collection": [resolved]}
            elif resolved.get("kind") == "playlist":
                tracks = resolved.get("tracks", [])
                # Set responses may have ID-only placeholders. Fetch in one bounded batch.
                missing = [str(t["id"]) for t in tracks[:200] if not t.get("permalink_url")]
                replacements = {}
                if missing:
                    replacements = {str(t["id"]): t for t in self.api("https://api-v2.soundcloud.com/tracks", query={"ids": ",".join(missing)})}
                data = {"collection": [replacements.get(str(t.get("id")), t) for t in tracks[:200]]}
            else:
                raise SourceError("Unsupported SoundCloud source")
        if not isinstance(data, dict) or not isinstance(data.get("collection"), list):
            raise SourceError("Invalid SoundCloud listing")
        entries = []
        skipped = 0
        for row in data["collection"]:
            raw = row.get("track") or row
            if row.get("playlist") or raw.get("kind") == "playlist":
                skipped += 1
                continue
            entry = normalize_track(raw, row.get("created_at") if "track" in row else None)
            if entry:
                entries.append(entry)
            else:
                skipped += 1
        return {"entries": entries, "next": clean_cursor(data.get("next_href")), "title": title, "skipped": skipped}

    def resolve_audio(self, track):
        progressive = track.get("progressive_url")
        if not progressive:
            raw = self.api("https://api-v2.soundcloud.com/resolve", query={"url": track["webpage_url"]})
            fresh = normalize_track(raw)
            progressive = fresh.get("progressive_url") if fresh else None
        if not progressive:
            raise SourceError("No full-length progressive MP3 is available")
        try:
            data = self.api(progressive)
        except SourceError:
            # Stored transcoding endpoints can change. Re-resolve once, never loop.
            raw = self.api("https://api-v2.soundcloud.com/resolve", query={"url": track["webpage_url"]})
            fresh = normalize_track(raw)
            if not fresh or not fresh.get("progressive_url") or fresh["progressive_url"] == progressive:
                raise
            track["progressive_url"] = fresh["progressive_url"]
            data = self.api(fresh["progressive_url"])
        url = data.get("url", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".sndcdn.com"):
            raise SourceError("Unexpected audio host")
        try:
            with self.session.get(url, headers={"Range": "bytes=0-0"}, timeout=(3, 5), stream=True) as response:
                response.raise_for_status()
                mime = response.headers.get("Content-Type", "").split(";")[0]
                content_range = response.headers.get("Content-Range", "")
                size = int(content_range.rsplit("/", 1)[1]) if content_range else int(response.headers.get("Content-Length", "0"))
                if mime != "audio/mpeg" or size <= 0:
                    raise SourceError("Audio is not a sized MP3 file")
        except (requests.RequestException, ValueError) as exc:
            raise SourceError("Audio availability check failed") from exc
        return {"url": url, "length": size, "type": "audio/mpeg", "expires_at": signed_url_expiry(url) or 0, "resolved_at": int(time.time())}

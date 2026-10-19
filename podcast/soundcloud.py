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
from yt_dlp.networking import Request

from podcast.artwork import episode_artwork


HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"}


class SourceError(RuntimeError):
    pass


class SourceDeadline(SourceError):
    pass


class DeadlineYoutubeDL(yt_dlp.YoutubeDL):
    """Bound each network operation during the first RSS request, including auth refresh."""
    def __init__(self, options, deadline=None):
        self.deadline = deadline
        super().__init__(options)

    def urlopen(self, req):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SourceDeadline("Initial preparation budget exhausted")
            if isinstance(req, str):
                req = Request(req)
            req.extensions["timeout"] = min(1.5, remaining)
        return super().urlopen(req)


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
    aac = next((x for x in sorted(raw.get("media", {}).get("transcodings", []),
                                 key=lambda x: x.get("quality") == "sq", reverse=True)
                if x.get("format", {}).get("protocol") == "hls"
                and x.get("format", {}).get("mime_type", "").split(";")[0] == "audio/mp4"
                and "mp4a.40.2" in x.get("format", {}).get("mime_type", "")
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
        "progressive_url": progressive.get("url", ""), "hls_url": aac.get("url", ""),
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
    def __init__(self, session=None, *, deadline=None):
        self.deadline = deadline
        self.session = session or requests.Session()
        self.ydl = DeadlineYoutubeDL({"quiet": True, "logger": QuietLogger(), "socket_timeout": 5,
                                     "retries": 0, "extractor_retries": 0, "cachedir": "/tmp/sc-podcast-yt-dlp"}, deadline)
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
        if getattr(self, "deadline", None) is not None and time.monotonic() >= self.deadline:
            raise SourceDeadline("Initial preparation budget exhausted")
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "api-v2.soundcloud.com":
            raise SourceError("Unexpected SoundCloud endpoint")
        try:
            return self.ie._call_api(url, "podcast", headers=self.ie._HEADERS, **kwargs)
        except Exception as exc:
            if getattr(self, "deadline", None) is not None and time.monotonic() >= self.deadline:
                raise SourceDeadline("Initial preparation budget exhausted") from exc
            raise SourceError("SoundCloud request failed") from exc

    def listing(self, feed, cursor=None):
        """Return a single durable page; no audio-format resolution."""
        if cursor:
            data = self.api(clean_cursor(cursor))
            title = "SoundCloud"
            image = None
        else:
            parts = feed.split("/")
            user_feed = len(parts) == 2 and parts[1] in ("likes", "reposts", "tracks")
            resolved = self.api("https://api-v2.soundcloud.com/resolve", query={"url": "https://soundcloud.com/" + (parts[0] if user_feed else feed)})
            title = resolved.get("username") or resolved.get("title") or parts[0]
            image = episode_artwork(resolved.get("artwork_url") or resolved.get("avatar_url") or resolved.get("user", {}).get("avatar_url") or "")
            if user_feed:
                title += {"likes": " · Likes", "tracks": " · Uploads", "reposts": " · Reposts"}[parts[1]]
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
        return {"entries": entries, "next": clean_cursor(data.get("next_href")), "title": title, "image": image, "skipped": skipped}

    def resolve_audio(self, track):
        # Refresh at most once if a retained transcoding endpoint disappeared.
        fresh = None
        endpoint = track.get("progressive_url") or track.get("hls_url")
        if not endpoint:
            fresh = normalize_track(self.api("https://api-v2.soundcloud.com/resolve", query={"url": track["webpage_url"]}))
            if fresh:
                track.update({k: fresh[k] for k in ("progressive_url", "hls_url", "duration")})
            endpoint = track.get("progressive_url") or track.get("hls_url")
        if not endpoint:
            raise SourceError("No full-length supported audio is available")
        try:
            data = self.api(endpoint)
        except SourceDeadline:
            raise
        except SourceError:
            if fresh is not None:
                raise
            fresh = normalize_track(self.api("https://api-v2.soundcloud.com/resolve", query={"url": track["webpage_url"]}))
            replacement = (fresh.get("progressive_url") or fresh.get("hls_url")) if fresh else None
            if not replacement or replacement == endpoint:
                raise
            track.update({k: fresh[k] for k in ("progressive_url", "hls_url", "duration")})
            endpoint = replacement
            data = self.api(endpoint)
        url = data.get("url", "")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not ((parsed.hostname or "").endswith(".sndcdn.com")
                        or parsed.hostname == "playback.media-streaming.soundcloud.cloud")):
            raise SourceError("Unexpected audio host")
        if not track.get("progressive_url"):
            return self.resolve_playlist(url)
        try:
            timeout = (3, 5)
            if getattr(self, "deadline", None) is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceDeadline("Initial preparation budget exhausted")
                timeout = (min(0.5, remaining / 2), min(1, remaining / 2))
            with self.session.get(url, headers={"Range": "bytes=0-0"}, timeout=timeout, stream=True) as response:
                response.raise_for_status()
                mime = response.headers.get("Content-Type", "").split(";")[0]
                content_range = response.headers.get("Content-Range", "")
                size = int(content_range.rsplit("/", 1)[1]) if content_range else int(response.headers.get("Content-Length", "0"))
                if mime != "audio/mpeg" or size <= 0:
                    raise SourceError("Audio is not a sized MP3 file")
        except (requests.RequestException, ValueError) as exc:
            raise SourceError("Audio availability check failed") from exc
        return {"url": url, "length": size, "type": "audio/mpeg", "expires_at": signed_url_expiry(url) or 0, "resolved_at": int(time.time())}

    def resolve_playlist(self, url):
        """Read bounded playlist metadata only; clients fetch all audio themselves."""
        deadline = min(getattr(self, "deadline", None) or float("inf"), time.monotonic() + 8)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceDeadline("Initial preparation budget exhausted")
        try:
            with self.session.get(url, stream=True, allow_redirects=False,
                                  timeout=(min(2, remaining / 2), min(3, remaining / 2))) as response:
                response.raise_for_status()
                mime = response.headers.get("Content-Type", "").split(";")[0].lower()
                if response.status_code != 200 or mime not in HLS_TYPES:
                    raise SourceError("Audio link is not an HLS playlist")
                body = bytearray()
                for chunk in response.iter_content(65536):
                    if time.monotonic() >= deadline:
                        raise SourceDeadline("Playlist metadata budget exhausted")
                    body.extend(chunk)
                    if len(body) > 8 * 1024 * 1024:
                        raise SourceError("Playlist metadata exceeds limit")
                if not body.startswith(b"#EXTM3U") or b"#EXT-X-ENDLIST" not in body:
                    raise SourceError("Audio link is not a complete VOD playlist")
        except requests.RequestException as exc:
            raise SourceError("Playlist availability check failed") from exc
        now = int(time.time())
        # This is the playlist's actual MIME/length, never a synthetic M4A file.
        return {"url": url, "delivery": "redirect", "length": len(body), "type": mime,
                "expires_at": signed_url_expiry(url) or now + 120, "resolved_at": now}

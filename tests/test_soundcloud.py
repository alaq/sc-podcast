import base64
import json
from types import SimpleNamespace

import pytest

from podcast.config import Config, normalize_feed
from podcast.soundcloud import DeadlineYoutubeDL, SoundCloud, SourceDeadline, SourceError, clean_cursor, epoch, normalize_track, signed_url_expiry


def raw_track():
    return {"id": 42, "kind": "track", "title": "DJ & Friends", "duration": 7200000,
            "created_at": "2020-01-01T00:00:00Z", "permalink_url": "https://soundcloud.com/dj/friends",
            "description": "A set", "user": {"username": "DJ", "avatar_url": "https://i1.sndcdn.com/avatar-large.jpg"},
            "media": {"transcodings": [{"url": "https://api-v2.soundcloud.com/preview/42", "snipped": True,
                      "format": {"protocol": "progressive", "mime_type": "audio/mpeg"}},
                      {"url": "https://api-v2.soundcloud.com/media/42", "format": {"protocol": "progressive", "mime_type": "audio/mpeg"}}]}}


def test_listing_uses_like_time_and_full_metadata_without_per_track_requests():
    source = object.__new__(SoundCloud)
    calls = []
    def api(url, **kwargs):
        calls.append(url)
        if url.endswith("/resolve"):
            return {"id": 99, "username": "Listener"}
        return {"collection": [{"created_at": "2026-09-05T10:00:00Z", "track": raw_track()}, {"playlist": {"kind": "playlist"}}],
                "next_href": "https://api-v2.soundcloud.com/users/99/likes?cursor=abc&client_id=secret"}
    source.api = api
    page = source.listing("listener/likes")
    entry = page["entries"][0]
    assert len(calls) == 2 and len(page["entries"]) == 1 and page["skipped"] == 1
    assert entry["liked_at"] == epoch("2026-09-05T10:00:00Z") > entry["original_published_at"]
    assert entry["duration"] == 7200 and entry["artwork"].endswith("-t3000x3000.jpg")
    assert entry["progressive_url"].endswith("/media/42") and "secret" not in page["next"]


def test_audio_range_probe_uses_total_size_and_refreshes_changed_endpoint():
    source = object.__new__(SoundCloud)
    calls = []
    raw = raw_track()
    def api(url, **kwargs):
        calls.append(url)
        if url.endswith("/stale"):
            raise SourceError("expired endpoint")
        if url.endswith("/resolve"):
            return raw
        return {"url": "https://cf-media.sndcdn.com/file.mp3?Expires=1900000000"}
    class Response:
        headers = {"Content-Type": "audio/mpeg", "Content-Length": "1", "Content-Range": "bytes 0-0/123456789"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
    def get(url, **kwargs):
        assert kwargs["stream"] is True and kwargs["headers"] == {"Range": "bytes=0-0"}
        return Response()
    source.api, source.session = api, SimpleNamespace(get=get)
    track = normalize_track(raw)
    track["progressive_url"] = "https://api-v2.soundcloud.com/stale"
    audio = source.resolve_audio(track)
    assert audio["length"] == 123456789 and audio["expires_at"] == 1900000000
    assert len(calls) == 3


def test_preview_only_tracks_are_rejected_and_cursor_cannot_leave_soundcloud():
    raw = raw_track()
    raw["media"]["transcodings"] = raw["media"]["transcodings"][:1]
    source = object.__new__(SoundCloud)
    source.api = lambda *a, **kw: raw
    with pytest.raises(SourceError, match="full-length"):
        source.resolve_audio(normalize_track(raw))
    with pytest.raises(SourceError):
        clean_cursor("https://attacker.invalid/collect")


def test_initial_network_operations_obey_remaining_budget(monkeypatch):
    requests = []
    monkeypatch.setattr("yt_dlp.YoutubeDL.urlopen", lambda self, req: requests.append(req))
    now = [100]
    monkeypatch.setattr("podcast.soundcloud.time.monotonic", lambda: now[0])
    ydl = object.__new__(DeadlineYoutubeDL)
    ydl.deadline = 107
    ydl.urlopen("https://api-v2.soundcloud.com/resolve")
    assert requests[-1].extensions["timeout"] == 1.5
    now[0] = 106.75
    ydl.urlopen("https://api-v2.soundcloud.com/resolve")
    assert requests[-1].extensions["timeout"] == 0.25
    now[0] = 107
    with pytest.raises(SourceDeadline):
        ydl.urlopen("https://api-v2.soundcloud.com/resolve")
    assert len(requests) == 2


def test_cloudfront_policy_expiry_and_legacy_dates():
    raw = json.dumps({"Statement": [{"Condition": {"DateLessThan": {"AWS:EpochTime": 1900000000}}}]}).encode()
    policy = base64.b64encode(raw).decode().replace("+", "-").replace("=", "_").replace("/", "~")
    assert signed_url_expiry("https://cf-media.sndcdn.com/file?Policy=" + policy) == 1900000000
    assert epoch('{"value":"1700000000"}') == 1700000000
    assert epoch("garbage") is None


def test_config_preview_isolation_and_allowed_origins(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_REF", "feat/persisted-feed")
    monkeypatch.setenv("PING_OVERCAST", "1")
    preview = Config.from_env()
    assert ":preview:" in preview.namespace and not preview.ping_overcast
    assert preview.base_for_host("attacker.invalid") == preview.bases[0]
    monkeypatch.setenv("VERCEL_ENV", "production")
    assert Config.from_env().namespace != preview.namespace
    monkeypatch.setenv("SC_FEEDS", " ")
    with pytest.raises(ValueError): Config.from_env()


@pytest.mark.parametrize("path", ["../../secret", "user/likes/extra", "user/%0a"])
def test_invalid_feed_paths(path):
    with pytest.raises(ValueError): normalize_feed(path)


def test_full_aac_hls_is_preferred_over_preview_and_redirected(monkeypatch):
    raw = raw_track()
    raw["media"]["transcodings"] = [
        {"url": "https://api-v2.soundcloud.com/preview/42", "snipped": True, "quality": "sq",
         "format": {"protocol": "hls", "mime_type": 'audio/mp4; codecs="mp4a.40.2"'}},
        {"url": "https://api-v2.soundcloud.com/aac/42", "quality": "sq",
         "format": {"protocol": "hls", "mime_type": 'audio/mp4; codecs="mp4a.40.2"'}}]
    track = normalize_track(raw)
    assert not track["progressive_url"] and track["hls_url"].endswith("/aac/42")
    source = object.__new__(SoundCloud)
    calls = []
    source.api = lambda url, **kw: calls.append(url) or {"url": "https://cf-media.sndcdn.com/index.m3u8"}
    source.session = playlist_session()
    audio = source.resolve_audio(track)
    assert audio["type"] == "application/vnd.apple.mpegurl"
    assert audio["delivery"] == "redirect" and audio["url"].endswith("index.m3u8")
    assert audio["length"] == len(PLAYLIST)
    assert calls == [track["hls_url"]]


def test_retained_mp3_endpoint_can_migrate_to_aac(monkeypatch):
    raw = raw_track()
    raw["media"]["transcodings"] = [{"url": "https://api-v2.soundcloud.com/aac/42",
        "format": {"protocol": "hls", "mime_type": 'audio/mp4; codecs="mp4a.40.2"'}}]
    source = object.__new__(SoundCloud)
    def api(url, **kwargs):
        if url.endswith("/old"): raise SourceError("Gone")
        if url.endswith("/resolve"): return raw
        return {"url": "https://cf-media.sndcdn.com/index.m3u8"}
    source.api = api
    source.session = playlist_session()
    track = {"webpage_url": raw["permalink_url"], "progressive_url": "https://api-v2.soundcloud.com/old"}
    assert source.resolve_audio(track)["type"] == "application/vnd.apple.mpegurl"
    assert track["progressive_url"].endswith("/old") and track["hls_url"].endswith("/aac/42")


PLAYLIST = (b'#EXTM3U\n#EXT-X-MAP:URI="https://cf-media.sndcdn.com/init"\n'
            b'#EXTINF:10,\nhttps://cf-media.sndcdn.com/audio.m4s\n#EXT-X-ENDLIST\n')


def playlist_session(body=PLAYLIST, mime="application/vnd.apple.mpegurl"):
    class Response:
        status_code = 200
        headers = {"Content-Type": mime}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield body
    def get(url, **kwargs):
        assert url.endswith("index.m3u8")  # No init or audio segment requests.
        assert kwargs["stream"] and not kwargs["allow_redirects"]
        return Response()
    return SimpleNamespace(get=get)


@pytest.mark.parametrize("body,mime", [
    (b"not a playlist", "application/vnd.apple.mpegurl"),
    (PLAYLIST.replace(b"#EXT-X-ENDLIST", b""), "application/vnd.apple.mpegurl"),
    (PLAYLIST, "audio/mp4"),
    (PLAYLIST + b"x" * (8 * 1024 * 1024), "application/vnd.apple.mpegurl"),
])
def test_playlist_checks_are_bounded_and_never_accept_media_as_playlist(body, mime):
    source = object.__new__(SoundCloud)
    source.session = playlist_session(body, mime)
    with pytest.raises(SourceError):
        source.resolve_playlist("https://cf-media.sndcdn.com/index.m3u8")


def test_temporary_mp3_resolution_failure_does_not_forget_working_legacy_link():
    raw = raw_track()
    raw['media']['transcodings'] = [{'url': 'https://api-v2.soundcloud.com/aac/42',
        'format': {'protocol': 'hls', 'mime_type': 'audio/mp4; codecs="mp4a.40.2"'}}]
    source = object.__new__(SoundCloud)
    attempts = []
    def api(url, **kwargs):
        if url.endswith('/resolve'): return raw
        if url.endswith('/old'):
            attempts.append(url)
            if len(attempts) == 1: raise SourceError('Temporary failure')
            return {'url': 'https://cf-media.sndcdn.com/original.mp3?Expires=1900000000'}
        return {'url': 'https://cf-media.sndcdn.com/index.m3u8'}
    class MP3Response:
        headers = {'Content-Type': 'audio/mpeg', 'Content-Range': 'bytes 0-0/123456789'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
    playlist_get = playlist_session().get
    def get(url, **kwargs):
        if '.mp3?' in url: return MP3Response()
        return playlist_get(url, **kwargs)
    source.api, source.session = api, SimpleNamespace(get=get)
    track = {'webpage_url': raw['permalink_url'], 'progressive_url': 'https://api-v2.soundcloud.com/old'}
    assert source.resolve_audio(track)['type'] == 'application/vnd.apple.mpegurl'
    assert track['progressive_url'].endswith('/old')
    recovered = source.resolve_audio(track)
    assert recovered['type'] == 'audio/mpeg' and recovered['length'] == 123456789
    assert len(attempts) == 2

import base64
import gzip
import hashlib
import http.client
import json
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import jwt
import pytest

from podcast.http import Handler
from podcast.migrate import publish
from podcast.registry import Registry, request_work
from podcast.soundcloud import SourceError
from podcast.sync import Synchronizer
from conftest import Source, track


@pytest.fixture
def server(config, store, source, feed):
    config = replace(config, signing_key="current-test-signing-key-long-enough", next_signing_key="next-test-signing-key-long-enough")
    Synchronizer(config, store, source).run(feed)
    publish(config, store, feed, no_consumer=True, minimum=8)
    class TestHandler(Handler):
        config_factory = staticmethod(lambda: config)
        store_factory = staticmethod(lambda _: store)
        source_factory = staticmethod(lambda **kwargs: source)
        def log_message(self, *args): pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def request(method="GET", path="/", body=None, headers=None):
        conn = http.client.HTTPConnection(*httpd.server_address, timeout=5)
        conn.request(method, path, body=body, headers={"Host": config.bases[0].split("//")[1], **(headers or {})})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result
    yield request, config
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_read_path_is_fast_stable_and_independent_of_soundcloud(server, source, store):
    request, config = server
    initial_calls = len(source.audio_calls), len(source.list_calls)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: request(), range(10)))
    assert all(x[0] == 200 and x[2] == results[0][2] for x in results)
    assert (len(source.audio_calls), len(source.list_calls)) == initial_calls
    items = ET.fromstring(results[0][2]).findall("./channel/item")
    assert len(items) == 8
    assert "s-maxage=300" in results[0][1]["Vercel-CDN-Cache-Control"]


def test_head_conditional_and_compression(server, store):
    request, _ = server
    status, headers, body = request()
    store.calls.clear()
    assert request("HEAD")[2] == b""
    assert request(headers={"If-None-Match": headers["ETag"]})[0] == 304
    assert request(headers={"If-Modified-Since": headers["Last-Modified"]})[0] == 304
    assert not any(":body:" in c[1] for c in store.calls if c[0] == "GET")
    assert request(headers={"If-None-Match": '"different"', "If-Modified-Since": headers["Last-Modified"]})[0] == 200
    status, compressed_headers, compressed = request(headers={"Accept-Encoding": "gzip"})
    assert status == 200 and compressed_headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(compressed) == body
    assert len(compressed) == int(compressed_headers["Content-Length"])
    assert "Content-Encoding" not in request(headers={"Accept-Encoding": "gzip;q=0"})[1]


def test_audio_get_head_are_direct_uncached_redirects(server):
    request, _ = server
    for method in ("GET", "HEAD"):
        status, headers, body = request(method, "/track/dj/set-200")
        assert status == 302 and headers["Location"].startswith("https://cf-media.sndcdn.com/")
        assert headers["Cache-Control"] == "no-store" and body == b""
    assert request(path="/track/dj/../extra")[0] == 404


def test_unknown_sources_and_public_status(server):
    request, _ = server
    assert request(path="/unknown/likes")[0] == 200  # prepares a usable first batch
    assert request(path="/api/sync")[0] == 405
    status, _, body = request(path="/status")
    assert status == 200 and json.loads(body)["published_count"] == 8
    page = request(path="/about")[2].decode()
    assert "overcast://" in page and "Follow a Show by URL" in page
    assert request(path="/art.png")[0] == 200


def test_new_source_prepares_incrementally_and_reuses_subscription(server, source):
    request, config = server
    before = request()[2]
    calls = len(source.list_calls), len(source.audio_calls)
    status, _, body = request("POST", "/api/feeds", json.dumps({"url": "https://soundcloud.com/robot-heart?utm_source=share"}))
    data = json.loads(body)
    assert status == 200 and data["feed"] == "robot-heart/tracks" and data["count"] == 0
    assert (len(source.list_calls), len(source.audio_calls)) == calls
    status, _, first = request(path="/robot-heart/tracks")
    assert status == 200
    first_ids = {x.findtext("guid") for x in ET.fromstring(first).findall("./channel/item")}
    assert len(first_ids) == 5
    auth = {"Authorization": "Bearer " + config.sync_secret}
    assert request("POST", "/api/sync", '{"feed":"robot-heart/tracks"}', auth)[0] == 200
    second = request(path="/robot-heart/tracks")[2]
    second_ids = {x.findtext("guid") for x in ET.fromstring(second).findall("./channel/item")}
    assert len(second_ids) == 13 and first_ids <= second_ids
    repeat = json.loads(request("POST", "/api/feeds", '{"url":"robot-heart/tracks"}')[2])
    assert repeat["count"] == 13 and repeat["feed_url"] == data["feed_url"]
    assert request()[2] == before
    assert json.loads(request(path="/api/feeds?source=robot-heart/tracks")[2])["state"] == "ready"
    assert b"Create podcast feed" in request(path="/add")[2]


def test_registration_rejects_external_urls_and_cross_origin_requests(server):
    request, config = server
    assert request("POST", "/api/feeds", '{"url":"https://example.com/collect"}')[0] == 400
    assert request("POST", "/api/feeds", '{"url":"robot-heart"}', {"Origin": "https://example.com"})[0] == 403
    assert request(path="/api/feeds?source=not-added/likes")[0] == 404


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_direct_cold_subscription_publishes_five_without_webpage(server, source, store, method):
    request, config = server
    feed = "direct/tracks"
    calls = len(source.audio_calls), len(source.list_calls)
    status, headers, body = request(method, "/" + feed)
    assert status == 200
    assert len(source.audio_calls) - calls[0] == 5
    assert len(source.list_calls) - calls[1] == 1
    assert Registry(config, store).get(feed)["automatic"]
    assert len(store.state(feed)["tracks"]) == 200
    second = request(path="/" + feed)
    items = ET.fromstring(second[2]).findall("./channel/item")
    assert len(items) == 5 and all(int(x.find("enclosure").get("length")) > 0 for x in items)
    assert int(headers["Content-Length"]) == len(second[2])
    assert body == (b"" if method == "HEAD" else second[2])
    assert len(source.audio_calls) - calls[0] == 5
    assert len(source.list_calls) - calls[1] == 1


def test_concurrent_first_subscribers_share_initial_preparation(server, source, monkeypatch):
    request, _ = server
    original = source.listing
    def slow_listing(*args):
        time.sleep(0.1)
        return original(*args)
    monkeypatch.setattr(source, "listing", slow_listing)
    calls = len(source.audio_calls), len(source.list_calls)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: request(path="/direct/tracks"), range(10)))
    assert all(r[0] == 200 and r[2] == results[0][2] for r in results)
    assert len(ET.fromstring(results[0][2]).findall("./channel/item")) == 5
    assert (len(source.audio_calls) - calls[0], len(source.list_calls) - calls[1]) == (5, 1)


def test_cold_source_failure_is_retryable_without_empty_feed_or_repeated_extraction(server, source):
    request, _ = server
    source.pages[None] = SourceError("Source unavailable")
    calls = len(source.list_calls)
    for _ in range(2):
        status, headers, body = request(path="/offline/tracks")
        assert status == 503 and headers["Retry-After"] == "5"
        assert b"<rss" not in body and b"request this feed again" in body
    assert len(source.list_calls) == calls + 1


def test_cold_rss_does_not_bypass_main_publication_gate(server, store, source, feed):
    request, _ = server
    store.command("DEL", store.key(feed, "manifest"))
    calls = len(source.list_calls)
    assert request()[0] == 503
    assert len(source.list_calls) == calls


@pytest.mark.parametrize("method,path,conditional", [
    ("GET", "/secondary/tracks", False),
    ("HEAD", "/secondary/tracks", False),
    ("GET", "/secondary/tracks", True),
    ("GET", "/api/feeds?source=secondary/tracks", False),
])
def test_secondary_requests_enqueue_stale_work_without_extracting(server, store, source, monkeypatch, method, path, conditional):
    request, config = server
    feed = "secondary/tracks"
    Registry(config, store).register(feed)
    Synchronizer(config, store, Source([track(1), track(2)])).run(feed, activate=True)
    initial = request(path="/secondary/tracks")
    status = store.status(feed)
    status.update(last_success_at=int(time.time()) - 3600, last_checked_at=int(time.time()) - 3600)
    store.command("SET", store.key(feed, "status"), json.dumps(status))
    sent = []
    monkeypatch.setattr("podcast.registry.requests.post", lambda *a, **kw: (sent.append(kw), nullcontext(SimpleNamespace(status_code=202)))[1])
    monkeypatch.setattr("podcast.http.request_work", lambda c, s, f, status=None: request_work(replace(c, qstash_token="test-token"), s, f, status))
    calls = len(source.list_calls), len(source.audio_calls)
    headers = {"If-None-Match": initial[1]["ETag"]} if conditional else {}
    result = request(method, path, headers=headers)
    assert result[0] == (304 if conditional else 200)
    assert len(sent) == 1 and sent[0]["json"]["feed"] == feed
    assert (len(source.list_calls), len(source.audio_calls)) == calls
    assert request(path="/secondary/tracks")[2] == initial[2]
    assert len(sent) == 1


def test_queue_outage_preserves_stale_secondary_rss(server, store, monkeypatch):
    import requests
    request, config = server
    feed = "secondary/tracks"
    Registry(config, store).register(feed)
    Synchronizer(config, store, Source([track(1)])).run(feed, activate=True)
    initial = request(path="/secondary/tracks")
    status = store.status(feed)
    status.update(last_success_at=1, last_checked_at=1)
    store.command("SET", store.key(feed, "status"), json.dumps(status))
    def fail(*a, **kw):
        assert kw["timeout"] == (0.5, 1.5)
        raise requests.Timeout("Queue unavailable")
    monkeypatch.setattr("podcast.registry.requests.post", fail)
    monkeypatch.setattr("podcast.http.request_work", lambda c, s, f, status=None: request_work(replace(c, qstash_token="test-token"), s, f, status))
    result = request(path="/secondary/tracks")
    assert result[0] == 200 and result[2] == initial[2]


def signature(config, raw, key=None, **claims):
    now = int(time.time())
    payload = {"iss": "Upstash", "sub": config.sync_url, "exp": now + 120, "nbf": now - 1,
               "body": base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()).decode().rstrip("="), **claims}
    return jwt.encode(payload, key or config.signing_key, algorithm="HS256")


def test_sync_requires_auth_before_any_source_work(server, source):
    request, config = server
    calls = len(source.list_calls)
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Upstash-Signature": "invalid"}):
        assert request("POST", "/api/sync", "{}", headers)[0] == 401
    assert len(source.list_calls) == calls
    assert request("POST", "/api/sync", "{}", {"Authorization": "Bearer " + config.sync_secret})[0] == 200


def test_qstash_key_rotation_body_url_and_expiry(server, source):
    request, config = server
    raw = '{"feed":"kado-nyc/likes"}'
    for key in (config.signing_key, config.next_signing_key):
        assert request("POST", "/api/sync", raw, {"Upstash-Signature": signature(config, raw, key)})[0] == 200
    for signed in (signature(config, "{}"), signature(config, raw, sub="https://attacker.invalid/api/sync"), signature(config, raw, exp=int(time.time()) - 60)):
        assert request("POST", "/api/sync", raw, {"Upstash-Signature": signed})[0] == 401
    assert request("POST", "/api/sync?wrong=1", raw, {"Upstash-Signature": signature(config, raw)})[0] == 404

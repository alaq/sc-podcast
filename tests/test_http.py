import base64
import gzip
import hashlib
import http.client
import json
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import ThreadingHTTPServer

import jwt
import pytest

from podcast.http import Handler
from podcast.migrate import publish
from podcast.sync import Synchronizer


@pytest.fixture
def server(config, store, source, feed):
    config = replace(config, signing_key="current-test-signing-key-long-enough", next_signing_key="next-test-signing-key-long-enough")
    Synchronizer(config, store, source).run(feed)
    publish(config, store, feed, no_consumer=True, minimum=8)
    class TestHandler(Handler):
        config_factory = staticmethod(lambda: config)
        store_factory = staticmethod(lambda _: store)
        source_factory = staticmethod(lambda: source)
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
    assert request(path="/unknown/likes")[0] == 404
    assert request(path="/api/sync")[0] == 405
    status, _, body = request(path="/status")
    assert status == 200 and json.loads(body)["published_count"] == 8
    page = request(path="/about")[2].decode()
    assert "overcast://" in page and "Follow a Show by URL" in page
    assert request(path="/art.png")[0] == 200


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

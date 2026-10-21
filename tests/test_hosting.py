import hashlib
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.exceptions import ClientError

from podcast import hosting
from podcast.hosting import PART_SIZE, R2Audio
from podcast.http import Handler
from podcast.soundcloud import SourceDeadline, SourceError
from podcast.sync import Synchronizer
from conftest import Source, track


@pytest.fixture
def hosted_config(config):
    return replace(config, r2_account_id="a" * 32, r2_access_key="access",
                   r2_secret_key="secret", r2_bucket="test-audio", budget_seconds=230)


class ObjectStore:
    def __init__(self):
        self.objects, self.parts, self.events = {}, {}, []
        self.fail_part = False

    def head_object(self, **kw):
        if kw["Key"] not in self.objects:
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return self.objects[kw["Key"]]

    def create_multipart_upload(self, **kw):
        self.pending = kw
        self.events.append("start")
        return {"UploadId": "upload"}

    def upload_part(self, **kw):
        if self.fail_part:
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 503}}, "UploadPart")
        self.parts[kw["PartNumber"]] = kw["Body"]
        return {"ETag": hashlib.md5(kw["Body"]).hexdigest()}

    def complete_multipart_upload(self, **kw):
        self.events.append("complete")
        body = b"".join(self.parts[i["PartNumber"]] for i in kw["MultipartUpload"]["Parts"])
        self.objects[kw["Key"]] = {"ContentLength": len(body), "ContentType": self.pending["ContentType"],
                                   "Metadata": self.pending["Metadata"], "body": body}

    def abort_multipart_upload(self, **kw):
        self.events.append("abort")
        self.parts.clear()


def test_upload_parts_are_bounded_and_failed_upload_never_becomes_visible(hosted_config, monkeypatch):
    client = ObjectStore()
    r2 = R2Audio(hosted_config, client)
    chunk = b"a" * (1024 * 1024)
    indexed = {"length": 19 * len(chunk), "duration": 12}
    monkeypatch.setattr(hosting.hls, "chunks", lambda *a, **kw: iter([chunk] * 19))
    r2.upload("key", indexed, time.monotonic() + 60)
    assert [len(x) for x in client.parts.values()] == [PART_SIZE, PART_SIZE, 3 * len(chunk)]
    assert client.objects["key"]["body"] == chunk * 19
    assert client.events == ["start", "complete"]
    client.fail_part = True
    with pytest.raises(ClientError):
        r2.upload("failed", indexed, time.monotonic() + 60)
    assert "failed" not in client.objects and client.events[-1] == "abort"


def test_short_stream_and_deadline_abort_before_publication(hosted_config, monkeypatch):
    client = ObjectStore()
    r2 = R2Audio(hosted_config, client)
    monkeypatch.setattr(hosting.hls, "chunks", lambda *a, **kw: iter([b"short"]))
    with pytest.raises(SourceError, match="incomplete"):
        r2.upload("key", {"length": 10, "duration": 12}, time.monotonic() + 60)
    assert client.events == ["start", "abort"] and not client.objects
    with pytest.raises(SourceDeadline):
        r2.upload("key", {"length": 10, "duration": 12}, time.monotonic() - 1)
    assert client.events == ["start", "abort"]


def test_completed_object_survives_source_outage_and_lost_state(hosted_config):
    client = ObjectStore()
    r2 = R2Audio(hosted_config, client)
    entry = track(1)
    key = r2.key(entry)
    client.objects[key] = {"ContentLength": 100, "ContentType": "audio/mp4",
                           "Metadata": {"complete": "1", "duration": "3600"}}
    source = Source([entry])
    source.bad.add("1")
    assert r2.prepare(entry, source, time.monotonic() + 60) == r2.info(key, 100)
    assert not source.audio_calls and not client.events


def test_private_download_signatures_match_head_and_get(hosted_config):
    r2 = R2Audio(hosted_config)
    key = r2.key(track(1))
    get = urlsplit(r2.link(key, "GET"))
    head = urlsplit(r2.link(key, "HEAD"))
    assert get.hostname.endswith(".r2.cloudflarestorage.com")
    assert get.path.endswith(key) and head.path == get.path
    get_query, head_query = parse_qs(get.query), parse_qs(head.query)
    assert get_query["X-Amz-Signature"] != head_query["X-Amz-Signature"]
    assert get_query["X-Amz-SignedHeaders"] == ["host"]  # Client can request any byte range.
    assert get_query["X-Amz-Expires"] == ["86400"]
    with pytest.raises(SourceError):
        r2.link("another-prefix/object.m4a")


def test_hosted_sync_preserves_identity_and_publishes_only_completed_files(hosted_config, store, feed, monkeypatch):
    source = Source([track(1), track(2)])
    source.pages[None]["entries"][0]["progressive_url"] = ""
    source.pages[None]["entries"][1]["progressive_url"] = ""
    calls = []
    def prepare(entry, source, deadline):
        calls.append(entry["id"])
        if entry["id"] == "2":
            raise SourceError("Upload unavailable")
        return {"delivery": "r2", "key": "audio-v1/1.m4a", "length": 12345, "type": "audio/mp4"}
    monkeypatch.setattr(hosting, "R2Audio", lambda config: SimpleNamespace(prepare=prepare, close=lambda: None))
    sync = Synchronizer(hosted_config, store, source)
    assert sync.run(feed, activate=True)["published"] == 1
    state = store.state(feed)
    assert "length" not in state["tracks"]["2"]
    first = state["tracks"]["1"]
    assert first["published_at"] == track(1)["liked_at"]
    assert first["enclosure_path"] == "dj/set-1" and first["hosted_key"] == "audio-v1/1.m4a"
    assert store.track("dj/set-1")["hosted_key"] == first["hosted_key"]
    assert sync.run(feed)["state"] == "unchanged"
    assert calls == ["2", "1"]


def test_hosted_initial_read_defers_audio_download(hosted_config, store, feed, monkeypatch):
    source = Source([track(1, progressive_url="", hls_url="https://api-v2.soundcloud.com/media/1")])
    monkeypatch.setattr(hosting, "R2Audio", lambda _: pytest.fail("RSS read must not upload audio"))
    result = Synchronizer(hosted_config, store, source).run(feed, initial=True, activate=True)
    assert result["published"] == 0 and not source.audio_calls


def test_hosted_initial_read_can_resolve_direct_file_without_r2(hosted_config, store, feed, monkeypatch):
    source = Source([track(1)])
    monkeypatch.setattr(hosting, "R2Audio", lambda _: pytest.fail("RSS read must not contact R2"))
    result = Synchronizer(hosted_config, store, source).run(feed, initial=True, activate=True)
    assert result["published"] == 1
    assert store.state(feed)["tracks"]["1"]["audio_type"] == "audio/mpeg"


def test_hosted_initial_read_defers_if_direct_url_resolves_to_hls(hosted_config, store, feed, monkeypatch):
    source = Source([track(1)])
    source.resolve_audio = lambda _: {"length": 10, "type": "application/vnd.apple.mpegurl"}
    monkeypatch.setattr(hosting, "R2Audio", lambda _: pytest.fail("RSS read must not contact R2"))
    result = Synchronizer(hosted_config, store, source).run(feed, initial=True, activate=True)
    assert result["published"] == 0
    assert "length" not in store.state(feed)["tracks"]["1"]


def test_unconfigured_secondary_feed_keeps_existing_delivery(hosted_config, store, monkeypatch):
    monkeypatch.setattr(hosting, "R2Audio", lambda _: pytest.fail("Hosting was not enabled for this feed"))
    assert Synchronizer(hosted_config, store, Source([track(1)])).run("secondary/tracks", activate=True)["published"] == 1


def test_secondary_feed_cannot_erase_shared_hosted_download(hosted_config, store, feed, monkeypatch):
    monkeypatch.setattr(hosting, "R2Audio", lambda _: SimpleNamespace(
        prepare=lambda *a: {"delivery": "r2", "key": "audio-v1/1.m4a", "length": 12345, "type": "audio/mp4"},
        close=lambda: None))
    Synchronizer(hosted_config, store, Source([track(1)])).run(feed, activate=True)
    Synchronizer(hosted_config, store, Source([track(1)])).run("secondary/tracks", activate=True)
    assert store.track("dj/set-1")["hosted_key"] == "audio-v1/1.m4a"
    # A reused permalink for a different source identity must not inherit audio.
    token = store.acquire("secondary/tracks")
    store.commit("secondary/tracks", token, {}, tracks=[track(2, enclosure_path="dj/set-1")])
    store.release("secondary/tracks", token)
    assert not store.track("dj/set-1")["hosted_key"]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_listener_redirect_does_not_fetch_source_or_object(hosted_config, store, monkeypatch, method):
    store.command("SET", store.track_key("dj/set-1"), '{"hosted_key":"audio-v1/1.m4a"}')
    signed = []
    def link(key, verb):
        signed.append((key, verb))
        return "https://account.r2.cloudflarestorage.com/bucket/file?signature=test"
    monkeypatch.setattr(hosting, "R2Audio", lambda _: SimpleNamespace(link=link, close=lambda: None))
    handler = object.__new__(Handler)
    handler.command = method
    handler.source_factory = lambda: pytest.fail("Listener request contacted SoundCloud")
    result = []
    handler.reply = lambda status, body=b"", **kwargs: result.append((status, body, kwargs))
    handler.audio(hosted_config, store, "dj/set-1")
    assert signed == [("audio-v1/1.m4a", method)]
    assert result[0][0:2] == (302, b"")
    assert result[0][2]["headers"]["Cache-Control"] == "no-store"


def test_r2_read_failure_does_not_fall_back_to_hls(hosted_config, store):
    from podcast.store import StorageError
    handler = object.__new__(Handler)
    handler.command = "GET"
    handler.source_factory = lambda: pytest.fail("Must preserve durable delivery during metadata outage")
    store.track = lambda _: (_ for _ in ()).throw(StorageError("offline"))
    with pytest.raises(StorageError):
        handler.audio(hosted_config, store, "dj/set-1")

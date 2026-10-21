"""Real, tiny AAC/fMP4 fixtures contain four seconds of generated silence."""
import base64
import struct
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from podcast import hls

FIXTURES = Path(__file__).parent / 'fixtures' / 'aac'
HOST = 'https://cf-media.sndcdn.com/'


@pytest.fixture
def media(monkeypatch):
    objects = {p.name: p.read_bytes() for p in FIXTURES.glob('*.m4s')}
    calls = []
    def fetch(url, start=None, end=None, **kwargs):
        calls.append((url, start, end))
        data = objects[urlsplit(url).path.lstrip('/')]
        return (data if start is None else data[start:end + 1]), len(data)
    monkeypatch.setattr(hls, 'fetch', fetch)
    names = sorted(n for n in objects if n.startswith('chunk'))
    headers = [hls.segment_header(HOST + n) for n in names]
    duration = sum(s['duration'] / s['scale'] for s in headers)
    objects['index.m3u8'] = ('#EXTM3U\n#EXT-X-MAP:URI="init-stream0.m4s"\n' +
                            ''.join(f'#EXTINF:{s["duration"] / s["scale"]},\n{n}\n' for s, n in zip(headers, names)) + '#EXT-X-ENDLIST\n').encode()
    return objects, calls, duration


def test_indexed_file_has_correct_length_timeline_and_movie_duration(media):
    objects, calls, duration = media
    audio = hls.prepare(HOST + 'index.m3u8', duration)
    prefix = base64.b64decode(audio['prefix'])
    body = b''.join(hls.chunks(audio, 0, audio['length'] - 1))
    assert len(body) == audio['length'] and audio['duration'] == duration
    top = list(hls.boxes(body))
    assert [k for k, _ in top] == [b'ftyp', b'moov', b'sidx'] + [b'moof', b'mdat'] * 3
    movie = dict(hls.boxes(dict(top)[b'moov']))[b'mvhd']
    assert struct.unpack_from('>I', movie, 16)[0] == round(duration * 1000)
    index = dict(top)[b'sidx']
    assert struct.unpack_from('>H', index, 22)[0] == 3
    assert sum(struct.unpack_from('>I', index, 24 + 12 * i)[0] for i in range(3)) == len(body) - len(prefix)
    assert all(end == 127 for _, start, end in calls if start == 0)


def test_ranges_are_byte_exact_across_prefix_and_fragment_boundaries(media):
    _, calls, duration = media
    audio = hls.prepare(HOST + 'index.m3u8', duration)
    full = b''.join(hls.chunks(audio, 0, audio['length'] - 1))
    prefix_len = len(base64.b64decode(audio['prefix']))
    for start, end in [(0, 0), (0, 127), (prefix_len - 5, prefix_len + 5),
                       (prefix_len + audio['segments'][0]['length'] - 2, len(full) - 1), (len(full) - 1, len(full) - 1)]:
        calls.clear()
        assert b''.join(hls.chunks(audio, start, end)) == full[start:end + 1]
        assert sum(e - s + 1 for _, s, e in calls) <= end - start + 1


def test_signed_url_refresh_does_not_change_file_identity(media):
    _, _, duration = media
    a = hls.prepare(HOST + 'index.m3u8?Expires=1', duration)
    b = hls.prepare(HOST + 'index.m3u8?Expires=2', duration)
    assert a['etag'] == b['etag']


@pytest.mark.parametrize('change', ['#EXT-X-KEY:METHOD=AES-128,URI="secret"', '#EXT-X-BYTERANGE:20', '#EXT-X-DISCONTINUITY', '#EXT-X-STREAM-INF:BANDWIDTH=96'])
def test_reject_unsupported_playlist_layouts(media, change):
    objects, _, duration = media
    objects['index.m3u8'] += (change + '\n').encode()
    with pytest.raises(hls.HLSError): hls.prepare(HOST + 'index.m3u8', duration)


def test_reject_preview_discontinuous_and_truncated_fragments(media):
    objects, _, duration = media
    with pytest.raises(hls.HLSError, match='full length'): hls.prepare(HOST + 'index.m3u8', duration + 120)
    original = objects['chunk-stream0-00002.m4s']
    objects['chunk-stream0-00002.m4s'] = objects['chunk-stream0-00001.m4s']
    with pytest.raises(hls.HLSError, match='Discontinuous'): hls.prepare(HOST + 'index.m3u8', duration)
    objects['chunk-stream0-00002.m4s'] = original[:-1]
    with pytest.raises(hls.HLSError, match='size'): hls.prepare(HOST + 'index.m3u8', duration)


@pytest.mark.parametrize('url', ['http://cf-media.sndcdn.com/a', 'https://evil.invalid/a', 'https://sndcdn.com.evil.invalid/a', 'https://user@cf-media.sndcdn.com/a', 'https://cf-media.sndcdn.com:444/a'])
def test_media_host_allowlist(url):
    with pytest.raises(hls.HLSError): hls.media_url(url)


@pytest.mark.parametrize('value,expected', [(None, (0, 99, False)), ('bytes=0-0', (0, 0, True)), ('bytes=95-', (95, 99, True)), ('bytes=-3', (97, 99, True)), ('bytes=90-999', (90, 99, True))])
def test_range_parsing(value, expected):
    assert hls.byte_range(value, 100) == expected


@pytest.mark.parametrize('value', ['bytes=100-', 'bytes=-0', 'bytes=4-2', 'bytes=0-1,5-8', 'bytes=-', 'garbage'])
def test_invalid_range(value):
    with pytest.raises(ValueError): hls.byte_range(value, 100)


def test_source_must_honor_exact_ranges_and_not_redirect(monkeypatch):
    class Response:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, n): return iter([b'x'])
    def get(*args, **kwargs):
        assert kwargs['allow_redirects'] is False
        return Response()
    monkeypatch.setattr(hls.requests, 'get', get)
    with pytest.raises(hls.HLSError, match='honor'): hls.fetch(HOST + 'file', 0, 0)


def test_transient_source_fetch_retries_once_but_rejects_bad_ranges(monkeypatch):
    calls = []
    def once(*args):
        calls.append(args)
        if len(calls) == 1: raise hls.TransientHLSError('Temporary failure')
        return b'ok', 2
    monkeypatch.setattr(hls, '_fetch_once', once)
    monkeypatch.setattr(hls.time, 'sleep', lambda _: None)
    assert hls.fetch(HOST + 'file') == (b'ok', 2)
    assert len(calls) == 2
    def fail(*args): raise hls.TransientHLSError('Still unavailable')
    monkeypatch.setattr(hls, '_fetch_once', fail)
    with pytest.raises(hls.TransientHLSError): hls.fetch(HOST + 'file')


def test_worker_reuses_its_connection_session_and_closes_it(monkeypatch):
    sessions = []
    class Response:
        status_code = 206
        headers = {'Content-Range': 'bytes 0-0/2'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, n): return iter([b'x'])
    class Session:
        def __init__(self):
            self.calls, self.closed = 0, False
            sessions.append(self)
        def get(self, *args, **kwargs):
            assert not self.closed
            self.calls += 1
            return Response()
        def close(self): self.closed = True
    monkeypatch.setattr(hls.requests, 'Session', Session)
    with hls.workers(1) as pool:
        assert list(pool.map(lambda _: hls.fetch(HOST + 'audio', 0, 0), range(3))) == [(b'x', 2)] * 3
    assert len(sessions) == 1 and sessions[0].calls == 3 and sessions[0].closed

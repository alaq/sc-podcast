"""Bounded assembly of indexed AAC/M4A files for object-storage preparation.

Only background preparation imports this module; listener requests redirect to
completed objects. Audio fragments are fetched with a bounded memory window.
"""
import base64
import hashlib
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import local, Lock
from collections import deque
from urllib.parse import urljoin, urlsplit

import requests

MAX_SEGMENTS = 8640
MAX_FRAGMENT = 4 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024 * 1024
CONCURRENCY = 12
HEADER_CONCURRENCY = 24
_http = local()


@contextmanager
def workers(count):
    """Reuse TLS connections within each worker, then release every session."""
    sessions, lock = [], Lock()
    def initialize():
        session = requests.Session()
        _http.session = session
        with lock:
            sessions.append(session)
    try:
        with ThreadPoolExecutor(max_workers=count, initializer=initialize) as pool:
            yield pool
    finally:
        for session in sessions:
            session.close()


class HLSError(RuntimeError):
    pass


def media_url(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.username or p.password or p.port or p.fragment
            or not (p.hostname == 'playback.media-streaming.soundcloud.cloud'
                    or (p.hostname or '').endswith('.sndcdn.com'))):
        raise HLSError('Unexpected media host')
    return url


class TransientHLSError(HLSError):
    pass


def fetch(url, start=None, end=None, limit=MAX_FRAGMENT, deadline=None):
    for attempt in range(2):
        try:
            return _fetch_once(url, start, end, limit, deadline)
        except TransientHLSError:
            if attempt or (deadline is not None and time.monotonic() + .25 >= deadline):
                raise
            time.sleep(.25)


def _fetch_once(url, start=None, end=None, limit=MAX_FRAGMENT, deadline=None):
    media_url(url)
    timeout = 5 if deadline is None else min(5, deadline - time.monotonic())
    if timeout <= 0:
        raise HLSError('Audio preparation deadline exceeded')
    headers = {'Accept-Encoding': 'identity'}
    if start is not None:
        headers['Range'] = f'bytes={start}-{end}'
    try:
        with getattr(_http, "session", requests).get(url, headers=headers, timeout=(min(2, timeout), timeout),
                          stream=True, allow_redirects=False) as response:
            if response.status_code in (429, 500, 502, 503, 504):
                raise TransientHLSError('Temporary audio source failure')
            total = None
            if start is not None:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if response.status_code != 206 or not match or tuple(map(int, match.groups()[:2])) != (start, end):
                    raise HLSError('Source did not honor the byte range')
                total = int(match[3])
                if total <= end:
                    raise HLSError('Invalid source size')
            elif response.status_code != 200:
                raise HLSError('Audio source unavailable')
            chunks, length = [], 0
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > limit or (deadline is not None and time.monotonic() >= deadline):
                    raise HLSError('Audio response exceeded its bound')
                chunks.append(chunk)
            if start is not None and length != end - start + 1:
                raise HLSError('Incomplete source range')
            return b''.join(chunks), total
    except requests.RequestException as exc:
        raise TransientHLSError('Audio source request failed') from exc


def boxes(data):
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            raise HLSError('Truncated MP4 box')
        size, kind = struct.unpack_from('>I4s', data, pos)
        if size < 8 or pos + size > len(data):
            raise HLSError('Invalid MP4 box')
        yield kind, data[pos + 8:pos + size]
        pos += size


def box(kind, body):
    return struct.pack('>I4s', len(body) + 8, kind) + body


def playlist(text, url):
    if not text.startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in text.splitlines():
        raise HLSError('A complete VOD playlist is required')
    init, segments, duration, pending = None, [], 0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('#EXT-X-BYTERANGE', '#EXT-X-DISCONTINUITY', '#EXT-X-STREAM-INF')):
            raise HLSError('Unsupported playlist layout')
        if line.startswith('#EXT-X-KEY:') and line != '#EXT-X-KEY:METHOD=NONE':
            raise HLSError('Encrypted audio is not supported')
        if line.startswith('#EXT-X-MAP:'):
            match = re.fullmatch(r'#EXT-X-MAP:URI="([^"]+)"', line)
            if init or not match:
                raise HLSError('Unsupported initialization map')
            init = media_url(urljoin(url, match[1]))
        elif line.startswith('#EXTINF:'):
            try:
                if pending is not None:
                    raise ValueError()
                pending = float(line.split(':', 1)[1].split(',')[0])
                if not 0 < pending <= 60:
                    raise ValueError()
            except ValueError as exc:
                raise HLSError('Invalid segment duration') from exc
        elif line and not line.startswith('#'):
            if pending is None or len(segments) >= MAX_SEGMENTS:
                raise HLSError('Invalid segment list')
            segments.append(media_url(urljoin(url, line)))
            duration += pending
            pending = None
    if not init or not segments or pending is not None:
        raise HLSError('Incomplete playlist')
    return init, segments, duration


def segment_header(url, deadline=None):
    data, total = fetch(url, 0, 127, limit=128, deadline=deadline)
    offset, index = 0, None
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        if kind == b'moof':
            if not index or total != offset + index['length'] or not 8 <= index['length'] <= MAX_FRAGMENT:
                raise HLSError('Unexpected fragment size')
            return {'url': url, 'skip': offset, **index}
        if kind not in (b'styp', b'sidx') or size < 8 or offset + size > len(data):
            raise HLSError('Unsupported fragment header')
        if kind == b'sidx':
            if index:
                raise HLSError('Duplicate segment index')
            body = data[offset + 8:offset + size]
            if len(body) < 24:
                raise HLSError("Truncated segment index")
            version = body[0]
            if version not in (0, 1) or body[1:4] != b'\0\0\0':
                raise HLSError('Unsupported segment index')
            ident, scale = struct.unpack_from('>II', body, 4)
            if len(body) != (44 if version else 36):
                raise HLSError('Unsupported segment references')
            fmt = '>QQ' if version else '>II'
            earliest, first = struct.unpack_from(fmt, body, 12)
            count_offset = 30 if version else 22
            if len(body) != count_offset + 14 or first or struct.unpack_from('>H', body, count_offset)[0] != 1:
                raise HLSError('Unsupported segment references')
            length, duration, sap = struct.unpack_from('>III', body, count_offset + 2)
            if length >> 31 or not duration or not scale or sap not in (0x80000000, 0x90000000):
                raise HLSError('Unsupported audio fragment')
            index = {'id': ident, 'scale': scale, 'time': earliest, 'duration': duration, 'length': length, 'sap': sap}
        offset += size
    raise HLSError('Missing audio fragment')


def initialization(data, total_ticks, scale, ident):
    """Fill durations in the original single-audio-track initialization segment."""
    try:
        top = dict(boxes(data))
        moov = list(boxes(top[b'moov']))
        if sum(k == b'trak' for k, _ in moov) != 1:
            raise HLSError('Expected one audio track')
        movie = next(v for k, v in moov if k == b'mvhd')
        movie_scale = struct.unpack_from('>I', movie, 20 if movie[0] else 12)[0]
        trak = dict(boxes(next(v for k, v in moov if k == b'trak')))
        tkhd = trak[b'tkhd']
        track_id = struct.unpack_from('>I', tkhd, 20 if tkhd[0] else 12)[0]
        mdia = dict(boxes(trak[b'mdia']))
        mdhd = mdia[b'mdhd']
        if (track_id != ident or mdia[b'hdlr'][8:12] != b'soun'
                or struct.unpack_from('>I', mdhd, 20 if mdhd[0] else 12)[0] != scale
                or b'mp4a' not in mdia[b'minf'] or b'enca' in mdia[b'minf']):
            raise HLSError('Expected unencrypted AAC audio')
        if not movie_scale:
            raise HLSError('Invalid movie timescale')
        movie_ticks = round(total_ticks * movie_scale / scale)
        def rewrite(content):
            result = []
            for kind, payload in boxes(content):
                payload = bytearray(payload)
                if kind in (b'moov', b'trak', b'mdia', b'edts'):
                    payload = rewrite(payload)
                elif kind in (b'mvhd', b'mdhd', b'tkhd'):
                    version = payload[0]
                    if version not in (0, 1):
                        raise HLSError('Unsupported MP4 duration')
                    offset = (28 if version else 20) if kind == b'tkhd' else (24 if version else 16)
                    struct.pack_into('>Q' if version else '>I', payload, offset,
                                     total_ticks if kind == b'mdhd' else movie_ticks)
                elif kind == b'elst':
                    version = payload[0]
                    if version not in (0, 1) or struct.unpack_from('>I', payload, 4)[0] != 1:
                        raise HLSError('Unsupported audio edit list')
                    struct.pack_into('>Q' if version else '>I', payload, 8, movie_ticks)
                result.append(box(kind, payload))
            return b''.join(result)
        return rewrite(data)
    except (KeyError, StopIteration, IndexError, struct.error) as exc:
        raise HLSError('Invalid audio initialization') from exc


def prepare(url, expected_duration, deadline=None):
    deadline = min(deadline or float("inf"), time.monotonic() + 220)
    raw, _ = fetch(url, limit=4 * 1024 * 1024, deadline=deadline)
    try:
        init_url, urls, duration = playlist(raw.decode('utf-8'), url)
    except UnicodeError as exc:
        raise HLSError('Invalid audio playlist') from exc
    if expected_duration and abs(duration - expected_duration) > max(2, expected_duration * .001):
        raise HLSError('Audio is not full length')
    init, _ = fetch(init_url, limit=65536, deadline=deadline)
    with workers(HEADER_CONCURRENCY) as pool:
        segments = list(pool.map(lambda u: segment_header(u, deadline), urls))
    ident, scale, ticks = segments[0]['id'], segments[0]['scale'], 0
    for segment in segments:
        if (segment['id'], segment['scale'], segment['time']) != (ident, scale, ticks):
            raise HLSError('Discontinuous audio timeline')
        ticks += segment['duration']
    if abs(ticks / scale - duration) > 1:
        raise HLSError('Segment durations do not match')
    init = initialization(init, ticks, scale, ident)
    index = struct.pack('>IIIIIHH', 0, ident, scale, 0, 0, 0, len(segments))
    for segment in segments:
        index += struct.pack('>III', segment['length'], segment['duration'], segment['sap'])
    prefix = init + box(b'sidx', index)
    length = len(prefix) + sum(s['length'] for s in segments)
    if length > MAX_FILE:
        raise HLSError('Audio file exceeds the supported size')
    fingerprint = hashlib.sha256(prefix + repr([(urlsplit(s['url']).path, s['length']) for s in segments]).encode()).hexdigest()
    return {'delivery': 'hls-m4a', 'type': 'audio/mp4', 'length': length,
            'duration': ticks / scale, 'etag': '"' + fingerprint + '"',
            'prefix': base64.b64encode(prefix).decode(),
            'segments': [{k: s[k] for k in ('url', 'skip', 'length')} for s in segments],
            'expires_at': int(time.time()) + 180, 'resolved_at': int(time.time())}


def byte_range(value, length):
    if not value:
        return 0, length - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
    if not match or not any(match.groups()):
        raise ValueError('Invalid range')
    left, right = match.groups()
    if not left:
        count = int(right)
        if not count:
            raise ValueError('Empty suffix')
        return max(0, length - count), length - 1, True
    start, end = int(left), min(int(right), length - 1) if right else length - 1
    if start >= length or start > end:
        raise ValueError('Unsatisfiable range')
    return start, end, True


def chunks(audio, start, end, deadline=None):
    prefix = base64.b64decode(audio['prefix'])
    if start < len(prefix):
        yield prefix[start:min(len(prefix), end + 1)]
    offset, work = len(prefix), []
    for segment in audio['segments']:
        last = offset + segment['length'] - 1
        if start <= last and end >= offset:
            work.append((segment['url'], segment['skip'] + max(0, start - offset),
                         segment['skip'] + min(segment['length'] - 1, end - offset)))
        offset = last + 1
        if offset > end:
            break
    # Bounded window rather than Executor.map, which would buffer the whole set
    # behind a slow listener. Each future contains at most one small fragment.
    with workers(CONCURRENCY) as pool:
        pending = deque()
        it = iter(work)
        def submit(item):
            return pool.submit(fetch, item[0], item[1], item[2], deadline=deadline)
        for _ in range(CONCURRENCY):
            item = next(it, None)
            if item is not None:
                pending.append(submit(item))
        try:
            while pending:
                yield pending.popleft().result()[0]
                item = next(it, None)
                if item is not None:
                    pending.append(submit(item))
        finally:
            for future in pending:
                future.cancel()

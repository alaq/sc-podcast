from http.server import BaseHTTPRequestHandler
import yt_dlp
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote, quote
import requests
import json
import os
import time
from pathlib import Path
import re
from html import unescape


def _load_local_env():
    """Best-effort loader for a project-level .env when running locally"""
    env_path = Path(__file__).resolve().parent.parent / '.env'
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        # Do not overwrite pre-existing env vars (e.g. when running on Vercel)
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()

# Vercel KV configuration
VERCEL_KV_REST_API_URL = os.environ.get('KV_REST_API_URL')
VERCEL_KV_REST_API_TOKEN = os.environ.get('KV_REST_API_TOKEN')

TRACK_METADATA_PREFIX = 'track-metadata'
TRACK_METADATA_VERSION = 'v1'
TRACK_METADATA_FIELDS = (
    'id',
    'title',
    'uploader',
    'uploader_id',
    'duration',
    'timestamp',
    'upload_date',
    'release_timestamp',
    'webpage_url',
    'description',
    'last_modified',
    'thumbnails',
)

try:
    DEFAULT_MAX_METADATA_REFRESHES = int(os.environ.get('MAX_TRACK_METADATA_REFRESHES', '5'))
except (TypeError, ValueError):
    DEFAULT_MAX_METADATA_REFRESHES = 5

def get_kv_key(feed_path, track_id):
    """Generate a unique key for a track in a specific feed"""
    return f"feed:{feed_path}:track:{track_id}"


def encode_kv_key(key):
    """Encode KV keys so we can safely use them in Upstash REST paths"""
    return quote(key, safe='')

def get_track_first_seen_time(feed_path, track_id):
    """Get the timestamp when this track was first seen in this feed"""
    if not VERCEL_KV_REST_API_URL or not VERCEL_KV_REST_API_TOKEN:
        return None

    def _coerce_timestamp(raw_value):
        """Convert KV value into an integer timestamp if possible"""
        if raw_value is None:
            return None

        if isinstance(raw_value, (int, float)):
            return int(raw_value)

        if isinstance(raw_value, dict):
            for candidate_key in ('value', 'result'):
                if candidate_key in raw_value:
                    nested = _coerce_timestamp(raw_value[candidate_key])
                    if nested is not None:
                        return nested
            # If nested lookup fails, fall back to any scalar-looking values
            for nested_value in raw_value.values():
                nested = _coerce_timestamp(nested_value)
                if nested is not None:
                    return nested

        if isinstance(raw_value, str):
            cleaned = raw_value.strip()

            # Handle values that come back wrapped in extra quotes
            if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) >= 2:
                cleaned = cleaned[1:-1]

            try:
                return int(float(cleaned))
            except (TypeError, ValueError):
                pass

            # As a last resort, attempt to parse JSON payloads
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, (int, float)):
                    return int(parsed)
                if isinstance(parsed, dict):
                    return _coerce_timestamp(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        return None

    try:
        key = get_kv_key(feed_path, track_id)
        encoded_key = encode_kv_key(key)
        headers = {
            'Authorization': f'Bearer {VERCEL_KV_REST_API_TOKEN}',
            'Content-Type': 'application/json'
        }

        response = requests.get(f'{VERCEL_KV_REST_API_URL}/get/{encoded_key}', headers=headers)

        if response.status_code == 200:
            data = response.json()
            return _coerce_timestamp(data.get('result'))
        elif response.status_code == 404:
            # Key doesn't exist, this is the first time we see this track
            return None
        else:
            print(f"Error fetching from KV: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Error accessing Vercel KV: {e}")
        return None

def set_track_first_seen_time(feed_path, track_id, timestamp):
    """Store the timestamp when this track was first seen in this feed"""
    if not VERCEL_KV_REST_API_URL or not VERCEL_KV_REST_API_TOKEN:
        return False
    
    try:
        key = get_kv_key(feed_path, track_id)
        encoded_key = encode_kv_key(key)
        headers = {
            'Authorization': f'Bearer {VERCEL_KV_REST_API_TOKEN}',
            'Content-Type': 'application/json'
        }

        data = {'value': str(timestamp)}
        response = requests.post(f'{VERCEL_KV_REST_API_URL}/set/{encoded_key}', 
                               headers=headers, json=data)
        
        if response.status_code == 200:
            return True
        else:
            print(f"Error storing to KV: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"Error storing to Vercel KV: {e}")
        return False


def vercel_kv_available():
    """Determine whether Vercel KV credentials are configured."""
    return bool(VERCEL_KV_REST_API_URL and VERCEL_KV_REST_API_TOKEN)


def _kv_headers():
    return {
        'Authorization': f'Bearer {VERCEL_KV_REST_API_TOKEN}',
        'Content-Type': 'application/json'
    }


def _decode_kv_result(response):
    """Decode a JSON payload returned by Vercel KV."""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return None

    result = payload.get('result')

    if isinstance(result, dict):
        return result

    if isinstance(result, (int, float)):
        return result

    if isinstance(result, str):
        cleaned = result.strip()
        if not cleaned:
            return None
        try:
            return json.loads(cleaned)
        except (TypeError, ValueError, json.JSONDecodeError):
            return cleaned

    return None


def coerce_epoch_seconds(value):
    """Attempt to convert various timestamp representations into epoch seconds."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        # Assume values that look like milliseconds are intended as seconds
        if value > 10**12:
            return int(value // 1000)
        return int(value)

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None

        # Raw integer-like string
        if cleaned.isdigit():
            numeric_value = int(cleaned)
            if len(cleaned) == 13:  # milliseconds
                return numeric_value // 1000
            if len(cleaned) == 8:  # YYYYMMDD
                try:
                    dt = datetime.strptime(cleaned, '%Y%m%d')
                    dt = dt.replace(tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    pass
            return numeric_value

        # ISO-8601-ish strings
        iso_candidate = cleaned
        if iso_candidate.endswith('Z'):
            iso_candidate = f"{iso_candidate[:-1]}+00:00"

        try:
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass

    return None


def sanitize_track_metadata(raw_metadata):
    """Reduce the yt_dlp metadata blob to the fields we care about."""
    if not isinstance(raw_metadata, dict):
        return None

    sanitized = {}
    for field in TRACK_METADATA_FIELDS:
        if field not in raw_metadata:
            continue
        value = raw_metadata.get(field)
        if value is None:
            continue
        if field == 'duration':
            try:
                sanitized[field] = int(value)
            except (TypeError, ValueError):
                continue
        elif field == 'thumbnails' and isinstance(value, list):
            cleaned_thumbnails = []
            for thumb in value:
                if not isinstance(thumb, dict):
                    continue
                url = thumb.get('url')
                if not url:
                    continue
                cleaned_thumbnails.append({
                    key: thumb.get(key)
                    for key in ('id', 'url')
                    if thumb.get(key) is not None
                })
            if cleaned_thumbnails:
                sanitized[field] = cleaned_thumbnails
        else:
            sanitized[field] = value

    if 'webpage_url' not in sanitized and raw_metadata.get('url'):
        sanitized['webpage_url'] = raw_metadata.get('url')

    return sanitized or None


def get_track_metadata(track_id):
    """Retrieve cached track metadata from Vercel KV."""
    if not vercel_kv_available() or not track_id:
        return None

    key = encode_kv_key(f"{TRACK_METADATA_PREFIX}:{TRACK_METADATA_VERSION}:{track_id}")

    try:
        response = requests.get(f'{VERCEL_KV_REST_API_URL}/get/{key}', headers=_kv_headers())

        if response.status_code == 200:
            return _decode_kv_result(response)
        if response.status_code == 404:
            return None

        print(f"Error fetching track metadata from KV: {response.status_code} - {response.text}")
        return None
    except Exception as exc:
        print(f"Error accessing track metadata in Vercel KV: {exc}")
        return None


def set_track_metadata(track_id, payload):
    """Persist sanitized track metadata to Vercel KV."""
    if not vercel_kv_available() or not track_id or not payload:
        return False

    key = encode_kv_key(f"{TRACK_METADATA_PREFIX}:{TRACK_METADATA_VERSION}:{track_id}")

    try:
        response = requests.post(
            f'{VERCEL_KV_REST_API_URL}/set/{key}',
            headers=_kv_headers(),
            json={'value': json.dumps(payload)}
        )

        if response.status_code == 200:
            return True

        print(f"Error storing track metadata to KV: {response.status_code} - {response.text}")
        return False
    except Exception as exc:
        print(f"Error storing track metadata in Vercel KV: {exc}")
        return False


def resolve_track_url(entry):
    """Derive a canonical SoundCloud track URL from a flat playlist entry."""
    if not isinstance(entry, dict):
        return None

    candidate = entry.get('webpage_url') or entry.get('url') or entry.get('webpage')
    if not candidate:
        return None

    if candidate.startswith('http://') or candidate.startswith('https://'):
        return candidate

    return f"https://soundcloud.com/{candidate.lstrip('/')}"


def fetch_track_metadata(track_url):
    """Fetch full track metadata with yt_dlp for a single track."""
    if not track_url:
        return None

    ydl_opts = {
        'format': 'bestaudio/best',
        'skip_download': True,
        'dump_single_json': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw = ydl.extract_info(track_url, download=False)
            return sanitize_track_metadata(raw)
    except Exception as exc:
        print(f"Error fetching metadata for track {track_url}: {exc}")
        return None


def merge_entry_with_metadata(entry, metadata):
    """Combine flat playlist details with cached metadata for RSS generation."""
    merged = dict(entry) if isinstance(entry, dict) else {}

    if metadata:
        for key, value in metadata.items():
            if value is None:
                continue
            if key == 'thumbnails' and not isinstance(value, list):
                continue
            merged[key] = value

    merged.setdefault('title', merged.get('name') or 'Unknown Title')
    merged.setdefault('uploader', 'Unknown Artist')

    track_url = merged.get('webpage_url') or merged.get('url')
    if not track_url:
        track_url = resolve_track_url(entry)
    if track_url:
        merged['webpage_url'] = track_url

    duration_value = merged.get('duration')
    if duration_value is None:
        merged['duration'] = 0
    else:
        try:
            merged['duration'] = int(float(duration_value))
        except (TypeError, ValueError):
            merged['duration'] = 0

    timestamp_candidates = (
        merged.get('timestamp'),
        merged.get('release_timestamp'),
        merged.get('epoch'),
        merged.get('last_modified'),
        merged.get('upload_date'),
    )
    resolved_timestamp = None
    for candidate in timestamp_candidates:
        resolved_timestamp = coerce_epoch_seconds(candidate)
        if resolved_timestamp is not None:
            break
    if resolved_timestamp is not None:
        merged['timestamp'] = resolved_timestamp

    description = merged.get('description')
    if description is None:
        merged['description'] = ''
    elif not isinstance(description, str):
        merged['description'] = str(description)

    return merged


def hydrate_track_entries(entries, max_refreshes=DEFAULT_MAX_METADATA_REFRESHES):
    """Hydrate flat playlist entries with cached (or freshly fetched) metadata."""
    if not entries:
        return []

    hydrated_entries = []
    remaining_refreshes = max(0, max_refreshes)

    for entry in entries:
        track_id = None
        if isinstance(entry, dict):
            track_id = entry.get('id') or entry.get('track_id') or entry.get('webpage_url')

        cached_record = get_track_metadata(track_id) if track_id else None
        metadata = None
        cached_last_modified = None

        if isinstance(cached_record, dict) and cached_record.get('version') == TRACK_METADATA_VERSION:
            metadata = cached_record.get('metadata')
            cached_last_modified = coerce_epoch_seconds(cached_record.get('last_modified'))

        entry_last_modified = None
        if isinstance(entry, dict):
            entry_last_modified = coerce_epoch_seconds(entry.get('last_modified'))
            if entry_last_modified is None:
                entry_last_modified = coerce_epoch_seconds(entry.get('timestamp'))

        needs_refresh = metadata is None
        if not needs_refresh and entry_last_modified and cached_last_modified and entry_last_modified > cached_last_modified:
            needs_refresh = True

        new_metadata = None
        if needs_refresh and remaining_refreshes > 0:
            track_url = resolve_track_url(entry)
            new_metadata = fetch_track_metadata(track_url)
            remaining_refreshes -= 1

            if new_metadata:
                metadata = new_metadata
                stored_last_modified = entry_last_modified
                if stored_last_modified is None:
                    stored_last_modified = coerce_epoch_seconds(new_metadata.get('last_modified'))
                if stored_last_modified is None:
                    stored_last_modified = coerce_epoch_seconds(new_metadata.get('timestamp'))
                payload = {
                    'version': TRACK_METADATA_VERSION,
                    'fetched_at': int(time.time()),
                    'last_modified': stored_last_modified,
                    'metadata': metadata,
                }
                if track_id:
                    set_track_metadata(track_id, payload)

        if metadata is None and isinstance(cached_record, dict):
            metadata = cached_record.get('metadata')

        hydrated_entries.append(merge_entry_with_metadata(entry, metadata))

    return hydrated_entries

def get_channel_info(channel_url, ydl_opts):
    """
    Try to extract channel information by making a separate request to the channel page
    """
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract just the channel info without entries
            channel_info = ydl.extract_info(channel_url, download=False, process=False)
            return channel_info
    except Exception:
        return None

def should_use_smart_timestamps(feed_path):
    """
    Determine if we should use smart timestamps (KV storage) for this feed type.
    Only applies to likes, reposts, and playlists/sets, not regular tracks.
    """
    return (
        '/likes' in feed_path or 
        '/reposts' in feed_path or 
        '/sets/' in feed_path or
        feed_path.endswith('/sets')
    )


def is_default_feed(feed_path):
    """Detect the special default feed that requires bespoke overrides."""
    if not feed_path:
        return False

    normalized = feed_path.split('?', 1)[0].rstrip('/').lower()
    return normalized == 'kado-nyc/likes'


def is_tracks_feed(feed_path):
    """Check whether the current feed path refers to a tracks feed."""
    if not feed_path:
        return False

    normalized = feed_path.split('?', 1)[0].rstrip('/').lower()
    return normalized.endswith('/tracks')


def title_contains_uploader(raw_title, uploader):
    """Detect if the uploader name already appears within the title."""
    if not raw_title or not uploader:
        return False

    if uploader.casefold() not in raw_title.casefold():
        return False

    boundary_pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(uploader)}(?![A-Za-z0-9])", re.IGNORECASE)
    return bool(boundary_pattern.search(raw_title))


def format_entry_title(item, feed_path):
    """Format the RSS item title based on feed type and uploader name."""
    raw_title = (item.get("title") or "Unknown Title").strip()

    if is_tracks_feed(feed_path):
        return raw_title or "Unknown Title"

    uploader = (item.get("uploader") or "").strip()
    if not uploader:
        return raw_title or "Unknown Title"

    # Strip noisy "<uploader> uploaded" prefixes that appear in likes/reposts feeds
    prefix = f"{uploader} uploaded "
    if raw_title.lower().startswith(prefix.lower()):
        raw_title = raw_title[len(prefix):].lstrip()

    if not raw_title:
        return f"{uploader} - Unknown Title"

    # Avoid duplicating the uploader if it's already present
    if title_contains_uploader(raw_title, uploader):
        return raw_title

    return f"{uploader} - {raw_title}"


def fetch_og_image_url(source_url):
    """Fetch the Open Graph image URL for the given SoundCloud page."""
    if not source_url:
        return None

    try:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0 Safari/537.36'
            )
        }
        response = requests.get(source_url, timeout=6, headers=headers)
        if response.status_code != 200:
            return None

        html_text = response.text
        for attribute in ('property', 'name'):
            pattern = re.compile(
                rf'<meta[^>]*{attribute}\s*=\s*["\']og:image["\'][^>]*>',
                re.IGNORECASE
            )
            match = pattern.search(html_text)
            if not match:
                continue

            tag = match.group(0)
            content_match = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
            if content_match:
                return unescape(content_match.group(1).strip())

        return None
    except Exception:
        return None


def create_podcast_xml(channel_info, server_url, feed_path, source_url):
    rss = ET.Element("rss", version="2.0", **{"xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"})
    channel = ET.SubElement(rss, "channel")
    
    # Get first entry to extract fallback information
    first_entry = None
    if channel_info.get("entries") and len(channel_info["entries"]) > 0:
        first_entry = channel_info["entries"][0]
    
    # Extract channel information with better fallbacks
    # Try to get channel name from multiple possible fields
    channel_name = (
        channel_info.get("title", "") or 
        channel_info.get("playlist_title", "") or
        channel_info.get("uploader", "") or
        (first_entry.get("uploader", "") if first_entry else "") or
        "Unknown Channel"
    )

    if is_default_feed(feed_path):
        channel_name = "ACSv3"
    
    # Try to get channel description
    channel_description = (
        channel_info.get("description", "") or
        channel_info.get("playlist_description", "") or
        channel_info.get("channel_description", "") or
        f"Podcast feed for {channel_name}"
    )
    
    # Try to get channel URL
    channel_url = (
        channel_info.get("webpage_url", "") or
        channel_info.get("uploader_url", "") or
        channel_info.get("channel_uploader_url", "") or
        ""
    )

    # Try to get channel author (uploader) with better fallbacks
    # For the default feed, force the desired author label regardless of metadata
    if is_default_feed(feed_path):
        channel_author = "ACSv3"
    else:
        # Extract account name from channel title
        raw_channel_name = channel_info.get("title", "")
        if raw_channel_name:
            if raw_channel_name == "kado (Likes)":
                channel_author = "ACS"
            # Remove (Tracks) suffix if present
            elif raw_channel_name.endswith(" (Tracks)"):
                channel_author = raw_channel_name[:-len(" (Tracks)")].strip()
            # Otherwise use the name as is
            else:
                channel_author = raw_channel_name.strip()
            
            # Fallback to "Unknown Author" if somehow empty
            if not channel_author:
                channel_author = "Unknown Author"
        else:
            # Fallback to original logic if no title
            channel_author = (
                channel_info.get("uploader", "") or
                channel_info.get("playlist_uploader", "") or
                channel_info.get("channel_uploader", "") or
                (first_entry.get("uploader", "") if first_entry else "") or
                "Unknown Author"
            )

    # Channel information
    ET.SubElement(channel, "title").text = channel_name
    ET.SubElement(channel, "link").text = channel_url
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "itunes:author").text = channel_author
    ET.SubElement(channel, "description").text = channel_description
    
    # Prefer OG-image from the SoundCloud page, fallback to static art
    if is_default_feed(feed_path):
        channel_artwork_url = f"{server_url}/art.png"
    else:
        channel_artwork_url = fetch_og_image_url(source_url) or f"{server_url}/art.png"
    ET.SubElement(channel, "itunes:image", href=channel_artwork_url)

    # Add items (tracks) to the channel
    for item in channel_info.get("entries", []):
        entry = ET.SubElement(channel, "item")
        formatted_title = format_entry_title(item, feed_path)
        ET.SubElement(entry, "title").text = formatted_title
        ET.SubElement(entry, "itunes:author").text = item.get("uploader", "Unknown Author")
        track_url = (item.get("webpage_url") or "").strip()

        description_text = item.get("description") or ""
        if track_url and track_url not in description_text:
            if description_text.strip():
                description_text = f"{description_text.rstrip()}\n\n{track_url}"
            else:
                description_text = track_url

        ET.SubElement(entry, "description").text = description_text

        # Determine publication date based on feed type
        if should_use_smart_timestamps(feed_path):
            # For likes, reposts, and playlists/sets: use smart timestamp tracking
            track_id = item.get("id", "") or item.get("webpage_url", "")
            
            # Get the first seen time for this track in this feed
            first_seen_time = None
            if track_id:
                first_seen_time = get_track_first_seen_time(feed_path, track_id)
                
                # If we haven't seen this track before, store the current time
                if first_seen_time is None:
                    current_time = int(time.time())
                    set_track_first_seen_time(feed_path, track_id, current_time)
                    first_seen_time = current_time
            
            # Use the first seen time for the feed, or fall back to current time (now)
            if first_seen_time is not None:
                pub_date = datetime.fromtimestamp(first_seen_time).strftime("%a, %d %b %Y %H:%M:%S GMT")
            else:
                # Fallback to current time if KV is unavailable or track_id is missing
                current_time = int(time.time())
                pub_date = datetime.fromtimestamp(current_time).strftime("%a, %d %b %Y %H:%M:%S GMT")
        else:
            # For regular tracks: use the original track publication timestamp
            pub_date = datetime.fromtimestamp(item.get("timestamp", 0)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        
        ET.SubElement(entry, "pubDate").text = pub_date
        
        # Generate server URL for this track instead of extracting MP3 URL
        if track_url:
            # Extract the track path from the SoundCloud URL
            parsed_track = urlparse(track_url)
            track_path = parsed_track.path.strip('/')
            # Create a server URL that points back to our server
            server_track_url = f"{server_url}/track/{quote(track_path)}"
            
            enclosure = ET.SubElement(entry, "enclosure", url=server_track_url, type="audio/mpeg")
            ET.SubElement(entry, "itunes:duration").text = str(int(item.get("duration", 0)))

        # Get track artwork from SoundCloud
        track_thumbnail = ""
        for thumbnail in item.get("thumbnails", []):
            if thumbnail.get("id") == "original":
                track_thumbnail = thumbnail.get("url", "")
                break
        
        # If no original thumbnail, try to get the largest one
        if not track_thumbnail and item.get("thumbnails"):
            # Sort thumbnails by size (if available) or take the last one
            thumbnails = item.get("thumbnails", [])
            if thumbnails:
                track_thumbnail = thumbnails[-1].get("url", "")
        
        if track_thumbnail:
            ET.SubElement(entry, "itunes:image", href=track_thumbnail)

    return ET.tostring(rss, encoding="unicode")

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(404)
            self.end_headers()
            return
        
        # Handle static PNG files (with or without query parameters)
        if self.path.startswith('/art.png'):
            filename = self.path.split('?')[0][1:]  # Remove leading slash and query params
            try:
                # Get the directory where this script is located
                script_dir = os.path.dirname(os.path.abspath(__file__))
                file_path = os.path.join(script_dir, filename)
                
                with open(file_path, 'rb') as f:
                    content = f.read()
                
                self.send_response(200)
                self.send_header('Content-type', 'image/png')
                self.send_header('Content-length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            except FileNotFoundError:
                self.send_response(404)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(f'File {filename} not found'.encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(f'Error serving {filename}: {str(e)}'.encode('utf-8'))
                return

        parsed_path = urlparse(self.path)
        path_parts = parsed_path.path.strip('/').split('/')
        
        # Handle track requests: /track/user/track-name
        if len(path_parts) >= 2 and path_parts[0] == 'track':
            track_path = '/'.join(path_parts[1:])  # Remove 'track' prefix
            track_url = f"https://soundcloud.com/{unquote(track_path)}"
            
            ydl_opts = {
                'format': 'bestaudio/best',
                'dump_single_json': True,
            }
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(track_url, download=False)
                    
                    # Find the best available HTTP audio format (no HLS)
                    audio_url = ""
                    formats = info.get("formats", [])
                    
                    # First, try to find any HTTP format (avoid HLS streaming)
                    for format in formats:
                        format_id = format.get("format_id", "")
                        if format_id.startswith("http_") and format.get("acodec") != "none" and format.get("url"):
                            audio_url = format.get("url", "")
                            break
                    
                    # If no HTTP format found, try any format with audio as fallback
                    if not audio_url:
                        for format in formats:
                            if format.get("acodec") != "none" and format.get("url"):
                                audio_url = format.get("url", "")
                                break
                    
                    if audio_url:
                        self.send_response(302)
                        self.send_header('Location', audio_url)
                        self.end_headers()
                    else:
                        self.send_response(404)
                        self.send_header('Content-type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'No audio format found')
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
            return
        
        # Handle playlist/channel requests (original behavior)
        channel_or_track = unquote(parsed_path.path.strip('/'))
        
        # If no path is provided, default to kado-nyc/likes
        if not channel_or_track:
            channel_or_track = "kado-nyc/likes"
        # If it's just a username (no /tracks, /likes, etc.), default to /tracks
        elif '/' not in channel_or_track:
            # It's just a username, append /tracks to get their tracks by default
            channel_or_track = f"{channel_or_track}/tracks"
        
        url = f"https://soundcloud.com/{channel_or_track}"
        
        # Get server URL for generating proper links
        server_url = f"https://{self.headers.get('Host', 'localhost')}"
        
        kv_enabled = vercel_kv_available()

        ydl_opts = {
            'format': 'bestaudio/best',
            'dump_single_json': True,
            'playlistend': 5,
        }

        if kv_enabled:
            ydl_opts['extract_flat'] = True
            ydl_opts['skip_download'] = True
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Check if it's a single track
                if 'entries' not in info:
                    # Convert single track to a list with one item
                    info['entries'] = [info]

                if kv_enabled:
                    info['entries'] = hydrate_track_entries(info.get('entries', []))
                
                # Try to get better channel information if we're missing key details
                if not info.get('description') and not info.get('thumbnails'):
                    # Extract the channel URL from the current URL
                    channel_url = f"https://soundcloud.com/{channel_or_track.split('/')[0]}"
                    
                    # Try to get channel info separately
                    channel_info = get_channel_info(channel_url, ydl_opts)
                    if channel_info:
                        # Merge channel info with playlist info
                        info['channel_description'] = channel_info.get('description', '')
                        info['channel_thumbnails'] = channel_info.get('thumbnails', [])
                        info['channel_uploader'] = channel_info.get('uploader', '')
                        info['channel_uploader_url'] = channel_info.get('uploader_url', '')
                
                # Debug: Print available fields in channel info (only in development)
                # You can uncomment these lines to see what fields are available
                # print("Channel info keys:", list(info.keys()))
                # print("Channel title:", info.get('title'))
                # print("Channel description:", info.get('description'))
                # print("Channel uploader:", info.get('uploader'))
                # print("Channel webpage_url:", info.get('webpage_url'))
                # print("Channel thumbnails:", info.get('thumbnails'))
                # print("Channel playlist_uploader:", info.get('playlist_uploader'))
                # print("Channel uploader_id:", info.get('uploader_id'))
                # print("Channel uploader_url:", info.get('uploader_url'))
                # if info.get('entries') and len(info['entries']) > 0:
                #     print("First entry keys:", list(info['entries'][0].keys()))
                #     print("First entry uploader:", info['entries'][0].get('uploader'))
                #     print("First entry thumbnails:", info['entries'][0].get('thumbnails'))
                
                podcast_xml = create_podcast_xml(info, server_url, channel_or_track, url)
                
                self.send_response(200)
                self.send_header('Content-type', 'application/rss+xml')
                self.end_headers()
                self.wfile.write(podcast_xml.encode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

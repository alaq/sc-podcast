from http.server import BaseHTTPRequestHandler
import yt_dlp
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse, unquote, quote
import requests
import json
import os
import time
from pathlib import Path
import re


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

def create_podcast_xml(channel_info, server_url, feed_path):
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

    print("channel_info:")
    # Debug: Let's see all available keys to find the account name
    print("Available channel_info keys:", list(channel_info.keys()))
    print("Available channel_info values:")
    for key in ['title', 'uploader', 'uploader_id', 'uploader_url', 'playlist_uploader', 'channel_uploader', 'channel', 'channel_id', 'channel_url', 'id', 'display_id']:
        value = channel_info.get(key, "")
        print(f"  {key}: '{value}'")
    
    if first_entry:
        print("First entry keys:", list(first_entry.keys()))
        print("First entry uploader info:")
        for key in ['uploader', 'uploader_id', 'uploader_url', 'channel', 'channel_id', 'channel_url']:
            value = first_entry.get(key, "")
            print(f"  {key}: '{value}'")
    
    # Try to get channel author (uploader) with better fallbacks
    # Extract account name from channel title
    raw_channel_name = channel_info.get("title", "")
    if raw_channel_name:
        # Special case: "kado (Likes)" becomes "ACS"
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
        
        print(f"Extracted account name: '{raw_channel_name}' -> '{channel_author}'")
    else:
        # Fallback to original logic if no title
        channel_author = (
            channel_info.get("uploader", "") or
            channel_info.get("playlist_uploader", "") or
            channel_info.get("channel_uploader", "") or
            (first_entry.get("uploader", "") if first_entry else "") or
            "Unknown Author"
        )
        print(f"Using fallback channel_author: '{channel_author}'")

    # Channel information
    ET.SubElement(channel, "title").text = channel_name
    ET.SubElement(channel, "link").text = channel_url
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "itunes:author").text = channel_author
    ET.SubElement(channel, "description").text = channel_description
    
    # Try to get channel artwork from multiple sources
    # channel_artwork = ""
    # 
    # # Check if there are channel-level thumbnails
    # if "thumbnails" in channel_info and channel_info["thumbnails"]:
    #     for thumb in channel_info["thumbnails"]:
    #         if thumb.get("url"):
    #             channel_artwork = thumb["url"]
    #             break
    # 
    # # Also check for channel thumbnails we might have extracted separately
    # if not channel_artwork and "channel_thumbnails" in channel_info:
    #     for thumb in channel_info["channel_thumbnails"]:
    #         if thumb.get("url"):
    #             channel_artwork = thumb["url"]
    #             break
    # 
    # # If still no artwork, try to get it from the first entry
    # if not channel_artwork and first_entry and first_entry.get("thumbnails"):
    #     for thumb in first_entry["thumbnails"]:
    #         if thumb.get("id") == "original":
    #             channel_artwork = thumb.get("url", "")
    #             break
    #     # If no original, get the largest/last thumbnail
    #     if not channel_artwork:
    #         thumbnails = first_entry.get("thumbnails", [])
    #         if thumbnails:
    #             channel_artwork = thumbnails[-1].get("url", "")
    # 
    # # If we found channel artwork, add it
    # if channel_artwork:
    #     ET.SubElement(channel, "itunes:image", href=channel_artwork)
    
    # Use static channel artwork
    channel_artwork_url = f"{server_url}/art.png"
    ET.SubElement(channel, "itunes:image", href=channel_artwork_url)

    # Add items (tracks) to the channel
    for item in channel_info.get("entries", []):
        entry = ET.SubElement(channel, "item")
        formatted_title = format_entry_title(item, feed_path)
        ET.SubElement(entry, "title").text = formatted_title
        ET.SubElement(entry, "itunes:author").text = item.get("uploader", "Unknown Author")
        ET.SubElement(entry, "description").text = item.get("description", "")
        
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
        track_url = item.get("webpage_url", "")
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
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'dump_single_json': True,
            'playlistend': 5,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Check if it's a single track
                if 'entries' not in info:
                    # Convert single track to a list with one item
                    info['entries'] = [info]
                
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
                
                podcast_xml = create_podcast_xml(info, server_url, channel_or_track)
                
                self.send_response(200)
                self.send_header('Content-type', 'application/rss+xml')
                self.end_headers()
                self.wfile.write(podcast_xml.encode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

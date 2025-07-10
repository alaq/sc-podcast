from http.server import BaseHTTPRequestHandler
import yt_dlp
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse, unquote, quote

def create_podcast_xml(channel_info, server_url):
    rss = ET.Element("rss", version="2.0", **{"xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"})
    channel = ET.SubElement(rss, "channel")
    channel_thumbnail = ""

    # Channel information
    ET.SubElement(channel, "title").text = channel_info.get("uploader", "Unknown Channel")
    ET.SubElement(channel, "link").text = channel_info.get("uploader_url", "")
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "itunes:author").text = channel_info.get("uploader", "Unknown Author")
    ET.SubElement(channel, "description").text = "SoundCloud channel podcast feed"

    # Add items (tracks) to the channel
    for item in channel_info.get("entries", []):
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = item.get("title", "Unknown Title")
        ET.SubElement(entry, "itunes:author").text = item.get("uploader", "Unknown Author")
        ET.SubElement(entry, "description").text = item.get("description", "")
        
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

        thumbnail = ""
        for thumbnail in item.get("thumbnails", []):
            if thumbnail.get("id") == "original":
                thumbnail = thumbnail.get("url", "")
                break
        if thumbnail:
            ET.SubElement(entry, "itunes:image", href=thumbnail)
            if channel_thumbnail == "":
                channel_thumbnail = thumbnail
                ET.SubElement(channel, "itunes:image", href=thumbnail)

    return ET.tostring(rss, encoding="unicode")

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(404)
            self.end_headers()
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
                        # Redirect to the actual audio URL
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
        
        # If it's just a username (no /tracks, /likes, etc.), default to /tracks
        if '/' not in channel_or_track:
            # It's just a username, append /tracks to get their tracks by default
            channel_or_track = f"{channel_or_track}/tracks"
        
        url = f"https://soundcloud.com/{channel_or_track}"
        
        # Get server URL for generating proper links
        server_url = f"http://{self.headers.get('Host', 'localhost')}"
        
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
                
                podcast_xml = create_podcast_xml(info, server_url)
                
                self.send_response(200)
                self.send_header('Content-type', 'application/rss+xml')
                self.end_headers()
                self.wfile.write(podcast_xml.encode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

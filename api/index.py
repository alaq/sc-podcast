from http.server import BaseHTTPRequestHandler
import yt_dlp
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse, unquote

def create_podcast_xml(channel_info):
    rss = ET.Element("rss", version="2.0", **{"xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"})
    channel = ET.SubElement(rss, "channel")

    print("channel_info", channel_info)

    # Channel information
    ET.SubElement(channel, "title").text = channel_info.get("uploader", "Unknown Channel")
    ET.SubElement(channel, "link").text = channel_info.get("uploader_url", "")
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "itunes:author").text = channel_info.get("uploader", "Unknown Author")
    ET.SubElement(channel, "description").text = "SoundCloud channel podcast feed"

    # Get the original thumbnail URL
    original_thumbnail = ""
    for thumbnail in channel_info.get("thumbnails", []):
        if thumbnail.get("id") == "original":
            original_thumbnail = thumbnail.get("url", "")
            break

    # Set channel thumbnail
    if original_thumbnail:
        ET.SubElement(channel, "itunes:image", href=original_thumbnail)

    print(len(channel_info.get("entries", [])))

    # Add items (tracks) to the channel
    for item in channel_info.get("entries", []):
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = item.get("title", "Unknown Title")
        ET.SubElement(entry, "itunes:author").text = item.get("uploader", "Unknown Author")
        ET.SubElement(entry, "description").text = item.get("description", "")
        
        pub_date = datetime.fromtimestamp(item.get("timestamp", 0)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        ET.SubElement(entry, "pubDate").text = pub_date
        
        # Find the HTTP MP3 format
        mp3_url = ""
        for format in item.get("formats", []):
            if format.get("format_id") == "http_mp3_128":
                mp3_url = format.get("url", "")
                break

        enclosure = ET.SubElement(entry, "enclosure", url=mp3_url, type="audio/mpeg")
        ET.SubElement(entry, "itunes:duration").text = str(int(item.get("duration", 0)))

        # Set episode thumbnail (using the original channel thumbnail)
        if original_thumbnail:
            ET.SubElement(entry, "itunes:image", href=original_thumbnail)

    return ET.tostring(rss, encoding="unicode")

def get_track_details(url):
    ydl_opts = {
        'format': 'bestaudio/best',
        'extract_flat': False,
        'dump_single_json': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(404)
            self.end_headers()
            return

        parsed_path = urlparse(self.path)
        channel_or_track = unquote(parsed_path.path.strip('/'))
        url = f"https://soundcloud.com/{channel_or_track}"
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'extract_flat': 'in_playlist',
            'dump_single_json': True,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Check if it's a single track
                if 'entries' not in info:
                    # Convert single track to a list with one item
                    info['entries'] = [info]
                else:
                    # Fetch detailed information for each track in the channel
                    detailed_entries = []
                    for entry in info['entries']:
                        track_url = entry['url']
                        detailed_entry = get_track_details(track_url)
                        detailed_entries.append(detailed_entry)
                    info['entries'] = detailed_entries
                
                podcast_xml = create_podcast_xml(info)
                
                self.send_response(200)
                self.send_header('Content-type', 'application/rss+xml')
                self.end_headers()
                self.wfile.write(podcast_xml.encode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

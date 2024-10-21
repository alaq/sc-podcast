from flask import Flask, Response
import yt_dlp
import xml.etree.ElementTree as ET
from datetime import datetime
import json

app = Flask(__name__)

def create_podcast_xml(channel_info):
    rss = ET.Element("rss", version="2.0", **{"xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"})
    channel = ET.SubElement(rss, "channel")

    # Channel information
    ET.SubElement(channel, "title").text = channel_info.get("uploader", "Unknown Channel")
    ET.SubElement(channel, "link").text = channel_info.get("uploader_url", "")
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "itunes:author").text = channel_info.get("uploader", "Unknown Author")
    ET.SubElement(channel, "description").text = "SoundCloud channel podcast feed"

    print(len(channel_info.get("entries", [])))

    # Add items (tracks) to the channel
    for item in channel_info.get("entries", []):
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = item.get("title", "Unknown Title")
        ET.SubElement(entry, "itunes:author").text = item.get("uploader", "Unknown Author")
        ET.SubElement(entry, "description").text = item.get("description", "")
        
        pub_date = datetime.fromtimestamp(item.get("timestamp", 0)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        ET.SubElement(entry, "pubDate").text = pub_date
        
        enclosure = ET.SubElement(entry, "enclosure", url=item.get("url", ""), type="audio/mpeg")
        ET.SubElement(entry, "itunes:duration").text = str(int(item.get("duration", 0)))

    return ET.tostring(rss, encoding="unicode")

@app.route('/<path:channel_name>')
def get_podcast(channel_name):
    url = f"https://soundcloud.com/{channel_name}"
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'extract_flat': 'in_playlist',
        'dump_single_json': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            podcast_xml = create_podcast_xml(info)
            return Response(podcast_xml, mimetype='application/rss+xml')
        except Exception as e:
            return str(e), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000)

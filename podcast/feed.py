"""Pure RSS generation: no SoundCloud requests or storage access."""

import re
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote
import xml.etree.ElementTree as ET

from podcast.config import DEFAULT_FEED
from podcast.artwork import episode_artwork

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def xml_text(value):
    return re.sub("[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", str(value or ""))


def title_for(track, feed):
    title = track.get("title") or "Untitled set"
    artist = track.get("uploader") or ""
    if feed.endswith("/tracks") or not artist:
        return title
    prefix = artist + " uploaded "
    if title.lower().startswith(prefix.lower()):
        title = title[len(prefix):]
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(artist)}(?![A-Za-z0-9])", title, re.I):
        return title
    return f"{artist} - {title}"


def enclosure_url(base, track):
    return base + "/track/" + quote(track["enclosure_path"], safe="/")


def render(feed, entries, base, title=None, image=None):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    def tag(parent, name, value, **attrs):
        element = ET.SubElement(parent, name, attrs)
        element.text = xml_text(value)
        return element
    name = "ACSv3" if feed == DEFAULT_FEED else (title or feed)
    tag(channel, "title", name)
    tag(channel, "link", "https://soundcloud.com/" + feed)
    tag(channel, "description", f"Podcast feed for {name}")
    tag(channel, "language", "en-us")
    tag(channel, f"{{{ITUNES}}}author", name)
    ET.SubElement(channel, f"{{{ITUNES}}}image", href=episode_artwork(image) if image and feed != DEFAULT_FEED else base + "/art.png")
    ET.SubElement(channel, f"{{{ATOM}}}link", href=base + ("/" if feed == DEFAULT_FEED else "/" + feed), rel="self", type="application/rss+xml")
    for track in entries:
        if not track.get("length") or not track.get("published_at"):
            raise ValueError("Only prepared episodes may be published")
        item = ET.SubElement(channel, "item")
        tag(item, "title", title_for(track, feed))
        tag(item, "link", track["webpage_url"])
        tag(item, f"{{{ITUNES}}}author", track.get("uploader"))
        description = track.get("description") or ""
        if track["webpage_url"] not in description:
            description = (description.rstrip() + "\n\n" + track["webpage_url"]).lstrip()
        tag(item, "description", description)
        url = enclosure_url(base, track)
        # Existing clients previously used the enclosure as their fallback ID.
        guid = track.get("guid_by_base", {}).get(base) or (url if track.get("legacy_identity") else "soundcloud:track:" + track["id"])
        tag(item, "guid", guid, isPermaLink="false")
        tag(item, "pubDate", format_datetime(datetime.fromtimestamp(track["published_at"], timezone.utc), usegmt=True))
        ET.SubElement(item, "enclosure", url=url, length=str(track["length"]), type="audio/mpeg")
        tag(item, f"{{{ITUNES}}}duration", int(track.get("duration") or 0))
        if track.get("artwork"):
            # Upgrade retained metadata too, including tracks outside the latest
            # SoundCloud listing. Rendering remains entirely local.
            ET.SubElement(item, f"{{{ITUNES}}}image", href=episode_artwork(track["artwork"]))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

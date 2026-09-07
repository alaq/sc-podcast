"""SoundCloud artwork URLs sized for Apple Podcasts' 1400–3000px requirement."""

import re
from urllib.parse import urlsplit, urlunsplit


def episode_artwork(url):
    parsed = urlsplit(url)
    if not (parsed.hostname or "").endswith(".sndcdn.com"):
        return url
    # SoundCloud supports this generated square rendition even when an artist's
    # original upload is smaller. An arbitrary size such as t1400x1400 is a 404.
    path = re.sub(r"-(?:large|t\d+x\d+)(\.(?:jpg|jpeg|png))$", r"-t3000x3000\1", parsed.path)
    return urlunsplit(parsed._replace(path=path))

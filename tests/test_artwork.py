import copy
import xml.etree.ElementTree as ET

import pytest

from podcast.artwork import episode_artwork
from podcast.feed import render
from conftest import track


@pytest.mark.parametrize("url, expected", [
    ("https://i1.sndcdn.com/artworks-set-large.jpg", "https://i1.sndcdn.com/artworks-set-t3000x3000.jpg"),
    ("https://i2.sndcdn.com/artworks-set-t500x500.png", "https://i2.sndcdn.com/artworks-set-t3000x3000.png"),
    ("https://i1.sndcdn.com/artworks-set-t3000x3000.jpg", "https://i1.sndcdn.com/artworks-set-t3000x3000.jpg"),
    ("https://example.com/artwork-t500x500.jpg", "https://example.com/artwork-t500x500.jpg"),
])
def test_episode_artwork_uses_supported_large_soundcloud_rendition(url, expected):
    assert episode_artwork(url) == expected


def test_retained_artwork_upgrades_without_changing_metadata_or_episode_identity(config, feed):
    saved = track(1, length=1234, published_at=1700000000,
                  artwork="https://i1.sndcdn.com/artworks-old-t500x500.jpg", legacy_identity=True)
    original = copy.deepcopy(saved)
    for base in config.bases:
        item = ET.fromstring(render(feed, [saved], base)).find("./channel/item")
        assert item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image").get("href").endswith("-t3000x3000.jpg")
        assert item.findtext("guid") == item.find("enclosure").get("url") == base + "/track/" + saved["enclosure_path"]
        assert item.findtext("pubDate") == "Tue, 14 Nov 2023 22:13:20 GMT"
    assert saved == original

import base64
from pathlib import Path

import pytest

from src.vision import build_image_content_blocks, has_enough_photos


def test_has_enough_photos_at_or_above_floor():
    assert has_enough_photos(5) is True
    assert has_enough_photos(20) is True


def test_has_enough_photos_below_floor():
    assert has_enough_photos(4) is False
    assert has_enough_photos(0) is False


def test_build_image_content_blocks_encodes_each_photo():
    calls = []

    def fake_read_bytes(path: Path) -> bytes:
        calls.append(path)
        return f"bytes for {path}".encode()

    paths = [Path("data/photos/abc/01.jpg"), Path("data/photos/abc/02.jpg")]

    blocks = build_image_content_blocks(paths, fake_read_bytes)

    assert len(blocks) == 2
    assert blocks[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(b"bytes for data/photos/abc/01.jpg").decode("utf-8"),
        },
    }
    assert calls == paths

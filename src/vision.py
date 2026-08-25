import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MIN_PHOTOS_FOR_VISION_SCORING = 5


def has_enough_photos(photo_count: int) -> bool:
    return photo_count >= MIN_PHOTOS_FOR_VISION_SCORING


def build_image_content_blocks(
    photo_paths: list[Path], read_bytes: Callable[[Path], bytes]
) -> list[dict]:
    """Base64-encodes each photo into a Claude vision content block.
    read_bytes is injected so this stays testable without real files."""
    blocks = []
    for path in photo_paths:
        data = base64.standard_b64encode(read_bytes(path)).decode("utf-8")
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
            }
        )
    return blocks

from pathlib import Path
from typing import Callable


def download_photos(
    photo_urls: list[str],
    dest_dir: Path,
    fetch_bytes: Callable[[str], bytes],
) -> list[Path]:
    """Download each photo to dest_dir/NN.jpg, skipping files that already
    exist. A failure fetching one photo is logged and skipped rather than
    aborting the rest of the listing's photos."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, url in enumerate(photo_urls, start=1):
        dest = dest_dir / f"{i:02d}.jpg"
        if dest.exists():
            saved.append(dest)
            continue
        try:
            dest.write_bytes(fetch_bytes(url))
        except Exception as exc:
            print(f"skip photo (failed to download {url}): {exc}")
            continue
        saved.append(dest)
    return saved

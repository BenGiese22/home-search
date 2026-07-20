import os
from pathlib import Path

from src.models import Listing


def render_gallery(listings: list[Listing], photos_root: Path, gallery_dir: Path) -> str:
    sections = []
    for listing in listings:
        photo_dir = photos_root / listing.listing_id
        photo_files = sorted(photo_dir.glob("*.jpg")) if photo_dir.exists() else []
        rel_srcs = [os.path.relpath(p, start=gallery_dir) for p in photo_files]
        photos_html = "".join(f'<img src="{src}" loading="lazy">' for src in rel_srcs)

        sections.append(
            f"""
        <section class="listing">
          <h2>{listing.address}, {listing.city}, {listing.state} {listing.zip_code}</h2>
          <p>{listing.price} &middot; {listing.beds} bd &middot; {listing.baths} ba &middot; {listing.sqft} sqft</p>
          <p>{listing.description}</p>
          <p>Amenities: {", ".join(listing.amenities)}</p>
          <p><a href="{listing.listing_url}">View on Compass</a></p>
          <div class="photos">{photos_html}</div>
        </section>"""
        )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Home Search Gallery</title>
<style>
  body {{ font-family: sans-serif; max-width: 900px; margin: 0 auto; padding: 16px; }}
  .listing {{ border-bottom: 1px solid #ccc; padding: 16px 0; }}
  .photos img {{ width: 200px; margin: 4px; border-radius: 4px; }}
</style>
</head>
<body>{"".join(sections)}</body>
</html>"""


def write_gallery(listings: list[Listing], photos_root: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_gallery(listings, photos_root, path.parent), encoding="utf-8")

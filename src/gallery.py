import html
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

        address = html.escape(listing.address)
        city = html.escape(listing.city)
        state = html.escape(listing.state)
        zip_code = html.escape(listing.zip_code)
        price = html.escape(listing.price)
        description = html.escape(listing.description)
        amenities = html.escape(", ".join(listing.amenities))
        listing_url = html.escape(listing.listing_url)
        # Omitted entirely when unknown -- showing "HOA: unknown" on ~95%
        # of listings would be noise, but a confirmed absence is worth
        # calling out since it's a genuine positive.
        if listing.hoa_annual is None:
            hoa_html = ""
        elif listing.hoa_annual == 0:
            hoa_html = "\n          <p>No HOA</p>"
        else:
            hoa_html = f"\n          <p>HOA ${listing.hoa_annual:,.0f}/yr</p>"

        sections.append(
            f"""
        <section class="listing">
          <h2>{address}, {city}, {state} {zip_code}</h2>
          <p>{price} &middot; {listing.beds} bd &middot; {listing.baths} ba &middot; {listing.sqft} sqft &middot; {listing.lot_sqft} lot sqft &middot; {listing.parking_spaces} parking</p>
          <p>{description}</p>
          <p>Amenities: {amenities}</p>{hoa_html}
          <p><a href="{listing_url}">View on Compass</a></p>
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

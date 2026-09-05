"""One-time: rename the desktop's downloaded photos onto the content key.

Photos on disk used to be `NN.jpg` and are now `NN-<hash8>.jpg`, where hash8
is sha1(source_url)[:8] -- see src/photos.py for why the position alone was
never an identity. Every glob in the project now ignores the old shape, so
until this runs the existing ~3,253 files are invisible: nothing counts them,
nothing scores them, and the next scrape re-downloads the lot.

The rename uses the same join as ops/backfill_hosted_source_urls.py, and the
same one-based/zero-based offset applies: the NN in a filename starts at 1,
photo_urls.position starts at 0.

A file whose position has no current URL is deleted rather than renamed. It
belongs to a set of photos the listing no longer serves -- exactly the stale
photos this whole change exists to stop -- and there is no URL to key it on.
Nothing here touches Blob: local files only.

Safe to run twice; already-migrated files are left alone.

    ./venv/bin/python ops/migrate_photo_files.py --dry-run   # see the plan
    ./venv/bin/python ops/migrate_photo_files.py             # do it
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.photo_upload import get_photo_urls_by_listing  # noqa: E402
from src.photos import photo_filename  # noqa: E402
from src.turso_db import stage_connection  # noqa: E402

PHOTOS_DIR = Path("data") / "photos"

# The shape this script migrates FROM. Anything else in the directory --
# already-renamed files included -- is left untouched.
OLD_FORMAT_GLOB = "[0-9][0-9]*.jpg"


def migrate_listing(listing_dir: Path, urls: list[str], apply: bool) -> tuple[int, int]:
    """Renames one listing's old-format files. Returns (renamed, orphaned).

    With apply=False nothing is written; the counts are what a real run would
    do, so a dry run is a plan rather than a guess.
    """
    renamed = 0
    orphaned = 0
    for path in sorted(listing_dir.glob(OLD_FORMAT_GLOB)):
        # `[0-9][0-9]*.jpg` also matches the new format; skip those so a
        # rerun is a no-op rather than a second rename.
        if "-" in path.stem:
            continue
        try:
            position = int(path.stem)
        except ValueError:
            continue
        # position is one-based on disk, zero-based in photo_urls.
        if position < 1 or position > len(urls):
            orphaned += 1
            print(f"  stale (no current URL at position {position}): {path}")
            if apply:
                path.unlink()
            continue
        dest = listing_dir / photo_filename(position, urls[position - 1])
        renamed += 1
        if apply:
            path.rename(dest)
    return renamed, orphaned


def main(argv: list[str]) -> None:
    apply = "--dry-run" not in argv
    if not apply:
        print("--dry-run: nothing will be written\n")
    if not PHOTOS_DIR.exists():
        print(f"{PHOTOS_DIR} does not exist; nothing to migrate")
        return

    conn = stage_connection()
    # One statement for every listing's URLs. B1 moves this function to
    # src/db.py under the same name.
    urls_by_listing = get_photo_urls_by_listing(conn)
    conn.close()

    total_renamed = 0
    total_orphaned = 0
    for listing_dir in sorted(p for p in PHOTOS_DIR.iterdir() if p.is_dir()):
        urls = urls_by_listing.get(listing_dir.name)
        if urls is None:
            # No listings row at all -- the listing was delisted and its row
            # pruned, but its directory survived. Reported, never deleted:
            # this script's job is renaming, and a surprise directory is
            # worth a human's eyes.
            print(f"  no listing row for {listing_dir.name}; left untouched")
            continue
        renamed, orphaned = migrate_listing(listing_dir, urls, apply)
        total_renamed += renamed
        total_orphaned += orphaned

    verb = "renamed" if apply else "would rename"
    print(f"\n{verb} {total_renamed} file(s); {total_orphaned} stale file(s)")


if __name__ == "__main__":
    main(sys.argv[1:])

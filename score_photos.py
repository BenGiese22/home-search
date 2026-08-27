# score_photos.py
import json
import time
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import dotenv_values

from src.db import (
    get_amenities,
    get_connection,
    get_listing_ids_missing_visual_score,
    query_listings,
    upsert_visual_score,
)
from src.photos import count_downloaded_photos
from src.vision import (
    MIN_PHOTOS_FOR_VISION_SCORING,
    VISUAL_SCORE_SCHEMA,
    build_image_content_blocks,
    has_enough_photos,
    parse_visual_response,
)

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "listings.db"
PHOTOS_DIR = DATA_DIR / "photos"
MODEL = "claude-sonnet-5"
MAX_TOKENS = 1536
POLL_INTERVAL_SECONDS = 60
# Checkpoints the in-flight batch id AND the garage_expected_by_id mapping
# used to submit it, between submission and the final upsert_visual_score
# calls. Storing garage_expected_by_id here (not just batch_id) matters: a
# crash partway through the results loop below leaves some listings already
# scored in the DB, which would make get_listing_ids_missing_visual_score()
# return a *narrower* set on resume than the batch actually contains -- the
# Batch API has no concept of partial consumption, so it always returns the
# full original result set. Rebuilding garage_expected_by_id from that
# narrower set would KeyError on the already-scored listings and silently
# null out their real scores. Loading it back from this checkpoint instead
# means resume always matches exactly what the batch was submitted with,
# regardless of how far a prior run got before crashing.
BATCH_STATE_PATH = DATA_DIR / ".photo_scoring_batch_state.json"


def _load_checkpoint() -> dict | None:
    if not BATCH_STATE_PATH.exists():
        return None
    return json.loads(BATCH_STATE_PATH.read_text())


def _save_checkpoint(batch_id: str, garage_expected_by_id: dict[str, bool]) -> None:
    BATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BATCH_STATE_PATH.write_text(
        json.dumps({"batch_id": batch_id, "garage_expected_by_id": garage_expected_by_id})
    )

# Ported from assess_six_houses.py's live-validated prompt (verified
# 2026-08-25 against a real "Virtual Staged" watermark in
# data/photos/2126174613662059081/03.jpg), minus the prototype's
# gut_reaction/overall_verdict/reasoning/notable_photo_observations fields --
# those existed only so that one-off script's output could be rendered into
# a markdown doc for comparison against Ben's in-person notes; nothing in
# the real pipeline reads free-text verdicts.
INSTRUCTIONS = (
    "You are assessing a real estate listing from its photos alone. Ignore any "
    "aerial/drone photos entirely -- whether they show this property or the "
    "surrounding neighborhood, they don't reliably show the home itself and "
    "shouldn't factor into any score or note below. For each room category "
    '(kitchen, bathrooms, living_space, garage), report status "present" with '
    "a 0-10 condition/quality score (10 = updated and move-in ready, 0 = dated "
    'or poorly maintained) if the photos clearly show it, or "omitted" if the '
    "room plausibly exists but no photo shows it. For basement, also allow "
    '"not_applicable" if the home clearly has no basement. Give each room a '
    "short one-sentence note explaining the score. For garage, also report "
    "whether it appears attached to the main house structure or a "
    "separate/detached building, when determinable from exterior photos "
    "(null if you can't tell). For the backyard, report whether it's shown, "
    "and if so rate tree/shade coverage and hosting/entertaining suitability, "
    "each 0-10, with a short note. Separately, report whether any photo is a "
    "floor-plan/layout graphic, and if so rate how legible and useful it is, "
    "0-10, with a short note.\n\n"
    "Staging: check every photo for a literal 'Virtual Staged' or 'Virtually "
    "Staged' watermark/caption burned into the image, and set "
    "watermarked_staging_detected accordingly. Separately, even where there's "
    "no watermark, look for visual signs a photo may still be virtually "
    "staged or unrealistically over-staged (furniture that looks slightly off "
    "in scale, perspective, or shadow direction; unnaturally crisp/rendered "
    "textures; empty-looking rooms with suspiciously perfect furniture) and "
    "set suspected_unwatermarked_staging accordingly. If either flag is true, "
    "treat that as a reason for lower confidence in the affected room(s)' "
    "scores -- let it pull those room scores toward the middle rather than "
    "taking staged photos at face value, and note what you saw."
)


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def build_listing_context(row, amenities: list[str]) -> str:
    return (
        f"Address: {row['address']}, {row['city']}, {row['state']} {row['zip_code']}\n"
        f"Price: {row['price']}\n"
        f"Beds: {row['beds']}, Baths: {row['baths']}, Sqft: {row['sqft']}, "
        f"Lot sqft: {row['lot_sqft']}, Parking spaces: {row['parking_spaces']}, "
        f"Year built: {row['year_built']}\n"
        f"Description: {row['description']}\n"
        f"Amenities: {', '.join(amenities)}"
    )


def build_batch_request(
    listing_id: str, row, amenities: list[str], photo_paths: list[Path]
) -> Request:
    content = build_image_content_blocks(photo_paths, read_bytes)
    listing_context = build_listing_context(row, amenities)
    content.append({"type": "text", "text": f"{listing_context}\n\n{INSTRUCTIONS}"})
    return Request(
        custom_id=listing_id,
        params=MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            output_config={"format": {"type": "json_schema", "schema": VISUAL_SCORE_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        ),
    )


def main() -> None:
    conn = get_connection(DB_PATH)
    env = dotenv_values(".env")
    if not env.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set in .env -- add it before running this script.")
        conn.close()
        return
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])

    checkpoint = _load_checkpoint()
    if checkpoint is not None:
        # Resuming after an interruption: the batch was already submitted
        # (and paid for) last run. Trust the checkpoint's garage_expected_by_id
        # as-is rather than recomputing it from the DB's current state -- see
        # the comment on BATCH_STATE_PATH for why that distinction matters.
        batch_id = checkpoint["batch_id"]
        garage_expected_by_id = checkpoint["garage_expected_by_id"]
        print(f"resuming existing batch {batch_id} (found {BATCH_STATE_PATH})")
    else:
        listings_by_id = {row["listing_id"]: row for row in query_listings(conn)}
        missing_ids = get_listing_ids_missing_visual_score(conn)

        if not missing_ids:
            print("visual_scores table already covers every listing")
            conn.close()
            return

        requests = []
        garage_expected_by_id = {}
        for listing_id in missing_ids:
            row = listings_by_id[listing_id]
            photo_count = count_downloaded_photos(PHOTOS_DIR, listing_id)
            if not has_enough_photos(photo_count):
                upsert_visual_score(conn, listing_id, None)
                print(
                    f"{listing_id}: skipped ({photo_count} photos, below floor of "
                    f"{MIN_PHOTOS_FOR_VISION_SCORING})"
                )
                continue
            photo_paths = sorted((PHOTOS_DIR / listing_id).glob("*.jpg"))
            amenities = get_amenities(conn, listing_id)
            garage_expected_by_id[listing_id] = row["parking_spaces"] > 0
            requests.append(build_batch_request(listing_id, row, amenities, photo_paths))

        if not requests:
            print("no listings had enough photos to score")
            conn.close()
            return

        batch = client.messages.batches.create(requests=requests)
        batch_id = batch.id
        _save_checkpoint(batch_id, garage_expected_by_id)
        print(f"submitted batch {batch_id} with {len(requests)} listings")

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        print(f"batch status: {batch.processing_status}, waiting {POLL_INTERVAL_SECONDS}s")
        time.sleep(POLL_INTERVAL_SECONDS)

    for result in client.messages.batches.results(batch_id):
        listing_id = result.custom_id
        try:
            garage_expected = garage_expected_by_id[listing_id]
            if result.result.type != "succeeded":
                upsert_visual_score(conn, listing_id, None)
                print(f"{listing_id}: batch item {result.result.type}")
                continue
            text = next(
                block.text for block in result.result.message.content if block.type == "text"
            )
            response_json = json.loads(text)
            visual_result = parse_visual_response(response_json, garage_expected)
        except Exception as exc:
            upsert_visual_score(conn, listing_id, None)
            print(f"{listing_id}: failed to parse response ({exc})")
            continue
        upsert_visual_score(conn, listing_id, visual_result, raw_response=json.dumps(response_json))
        staging_flag = (
            " [STAGING FLAGGED]"
            if visual_result.watermarked_staging_detected or visual_result.suspected_unwatermarked_staging
            else ""
        )
        print(
            f"{listing_id}: condition={visual_result.condition_photo_score:.0f} "
            f"outdoor={visual_result.outdoor_photo_score:.0f}{staging_flag}"
        )

    BATCH_STATE_PATH.unlink(missing_ok=True)
    conn.close()


if __name__ == "__main__":
    main()

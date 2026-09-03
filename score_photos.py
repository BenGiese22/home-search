# score_photos.py
import json
import sys
import time
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from src.config import load_env
from src.turso_db import stage_connection
from src.db import (
    get_amenities,
    get_listing_ids_missing_visual_score,
    query_listings,
    upsert_visual_score,
)
from src.photos import PHOTO_GLOB, count_downloaded_photos
from src.vision import (
    MIN_PHOTOS_FOR_VISION_SCORING,
    VISUAL_SCORE_SCHEMA,
    build_image_content_blocks,
    has_enough_photos,
    parse_visual_response,
)

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
MODEL = "claude-sonnet-5"
# 1536 was too small: confirmed live, 2026-08-27, that ~30% of listings hit
# stop_reason="max_tokens" before finishing all 8 schema fields, producing
# truncated, unparseable JSON. The room-by-room "notes" text can run long
# enough that the smaller production schema (no gut_reaction/reasoning/etc.
# fields, unlike the assess_six_houses.py prototype) still isn't a safe fit
# at 1536. Raised with real margin -- the cost difference is negligible
# next to the image-input tokens that dominate this request's cost anyway.
MAX_TOKENS = 4096
POLL_INTERVAL_SECONDS = 60
# The Message Batches API rejects a single batch-create request over 256MB
# (confirmed live, 2026-08-26: submitting ~142 listings' photos in one
# request hit a 413 request_too_large before any cost was incurred). Photos
# are sent base64-encoded (~4/3 the raw byte size) alongside a small amount
# of JSON/schema overhead per request, so this threshold is set well under
# the hard cap to leave real margin rather than tune close to the edge.
MAX_BATCH_REQUEST_BYTES = 180_000_000
# Per-request estimated overhead beyond the base64 photo payload itself
# (the schema, instructions text, and JSON structure) -- small relative to
# image bytes, but included so the size estimate isn't purely image-based.
REQUEST_OVERHEAD_BYTES = 5_000

# Checkpoints every already-submitted batch's id AND the garage_expected_by_id
# mapping used to submit it, as a list -- one batch can no longer cover every
# listing in one request (see MAX_BATCH_REQUEST_BYTES above), so a single-batch
# checkpoint isn't enough. Storing garage_expected_by_id per batch (not just
# batch_id) matters: a crash partway through the results loop below leaves
# some listings already scored in the DB, which would make
# get_listing_ids_missing_visual_score() return a *narrower* set on resume
# than a given batch actually contains -- the Batch API has no concept of
# partial consumption, so it always returns the full original result set for
# that batch. Rebuilding garage_expected_by_id from that narrower set would
# KeyError on the already-scored listings and silently null out their real
# scores. Loading it back from this checkpoint instead means every batch's
# results always resolve against exactly what it was submitted with,
# regardless of how far a prior run got before crashing -- and listing_ids
# already covered by an already-submitted batch are excluded from what still
# needs submitting on resume, so nothing is submitted (and paid for) twice.
BATCH_STATE_PATH = DATA_DIR / ".photo_scoring_batch_state.json"


def _load_checkpoint() -> list[dict]:
    if not BATCH_STATE_PATH.exists():
        return []
    return json.loads(BATCH_STATE_PATH.read_text())


def _append_checkpoint(batch_id: str, garage_expected_by_id: dict[str, bool]) -> None:
    batches = _load_checkpoint()
    batches.append({"batch_id": batch_id, "garage_expected_by_id": garage_expected_by_id})
    BATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BATCH_STATE_PATH.write_text(json.dumps(batches))


def _clear_checkpoint() -> None:
    BATCH_STATE_PATH.unlink(missing_ok=True)


def _chunk_by_size(entries: list[tuple], max_bytes: int) -> list[list[tuple]]:
    """Groups (listing_id, request, garage_expected, size_estimate) entries
    into chunks whose summed size_estimate stays under max_bytes. A single
    entry larger than max_bytes on its own still gets its own chunk rather
    than being dropped -- the Batch API's per-request-item limits are far
    smaller than a whole listing's photos, so this is a defensive fallback,
    not an expected case."""
    chunks: list[list[tuple]] = []
    current: list[tuple] = []
    current_size = 0
    for entry in entries:
        size = entry[3]
        if current and current_size + size > max_bytes:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(entry)
        current_size += size
    if current:
        chunks.append(current)
    return chunks

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
    "taking staged photos at face value, and note what you saw.\n\n"
    "Grounding: where the listing context above states a fact -- whether a "
    "basement exists, which outdoor features are present -- treat it as more "
    'reliable than your read of the photos. Use it to choose between "omitted" '
    'and "not_applicable", and note any contradiction rather than silently '
    "overriding it."
)


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _optional(row, key):
    """Row access that tolerates a pre-migration db (sqlite3.Row raises
    IndexError rather than returning None for an unknown column)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def build_listing_context(row, amenities: list[str]) -> str:
    """Grounds the vision model in what the listing data already asserts.

    Structured outdoor features and the basement split matter here because
    the model is asked to judge exactly those things from photos: whether a
    backyard is shown and how suitable it is for hosting, and whether a
    basement exists at all (its "not_applicable" option). Description is
    empty on 78 of 85 listings, so without these the model was being asked
    those questions with almost no context.
    """
    lines = [
        f"Address: {row['address']}, {row['city']}, {row['state']} {row['zip_code']}",
        f"Price: {row['price']}",
        f"Beds: {row['beds']}, Baths: {row['baths']}, Sqft: {row['sqft']}, "
        f"Lot sqft: {row['lot_sqft']}, Parking spaces: {row['parking_spaces']}, "
        f"Year built: {row['year_built']}",
    ]

    above = _optional(row, "sqft_above_grade")
    if above is not None:
        below = _optional(row, "sqft_below_grade")
        if below is None:
            lines.append(
                f"Finished area: {above:,} sqft above grade. "
                f"The listing data reports NO BASEMENT."
            )
        else:
            lines.append(
                f"Finished area: {above:,} sqft above grade, {below:,} sqft finished "
                f"below grade (a basement exists)."
            )

    outdoor_raw = _optional(row, "outdoor_spaces")
    outdoor = json.loads(outdoor_raw) if outdoor_raw else []
    if outdoor:
        lines.append(f"Outdoor features per the listing data: {', '.join(outdoor)}")

    lines.append(f"Description: {row['description']}")
    lines.append(f"Amenities: {', '.join(amenities)}")
    return "\n".join(lines)


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


def _process_batch_results(
    client: anthropic.Anthropic,
    conn,
    batch_id: str,
    garage_expected_by_id: dict[str, bool],
) -> None:
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


def main() -> None:
    conn = stage_connection()
    env = load_env()
    if not env.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set in .env -- add it before running this script.")
        conn.close()
        return
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])

    # Batches already submitted (this run or a prior, interrupted one) --
    # trusted as-is, never recomputed from the DB's current state. See the
    # comment on BATCH_STATE_PATH for why that distinction matters.
    submitted_batches = _load_checkpoint()
    already_submitted_ids = {
        listing_id
        for batch_entry in submitted_batches
        for listing_id in batch_entry["garage_expected_by_id"]
    }
    if submitted_batches:
        print(f"resuming {len(submitted_batches)} already-submitted batch(es) (found {BATCH_STATE_PATH})")

    listings_by_id = {row["listing_id"]: row for row in query_listings(conn)}
    # --rescore-all re-scores every listing, not just those without a score.
    # Needed whenever the prompt or the rubric changes: without it, a corpus
    # ends up scored under two different prompts, and a ranking built on a
    # mixed rubric is wrong in a way nothing surfaces. It costs real money
    # (every listing pays the vision API again), so it is opt-in.
    # already_submitted_ids is still honoured -- the checkpoint's whole job is
    # making sure an interrupted run never pays for the same listing twice.
    rescore_all = "--rescore-all" in sys.argv
    candidate_ids = (
        list(listings_by_id) if rescore_all else get_listing_ids_missing_visual_score(conn)
    )
    missing_ids = [
        listing_id
        for listing_id in candidate_ids
        if listing_id not in already_submitted_ids
    ]
    if rescore_all:
        print(f"--rescore-all: re-scoring {len(missing_ids)} listing(s)")

    pending_entries = []  # (listing_id, request, garage_expected, size_estimate)
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
        # PHOTO_GLOB, not *.jpg: an un-migrated NN.jpg may be a previous
        # listing's photo at this id, and sending it to the vision API
        # would score the wrong house and cost real money doing it.
        photo_paths = sorted((PHOTOS_DIR / listing_id).glob(PHOTO_GLOB))
        amenities = get_amenities(conn, listing_id)
        garage_expected = row["parking_spaces"] > 0
        request = build_batch_request(listing_id, row, amenities, photo_paths)
        size_estimate = (
            sum(p.stat().st_size for p in photo_paths) * 4 // 3 + REQUEST_OVERHEAD_BYTES
        )
        pending_entries.append((listing_id, request, garage_expected, size_estimate))

    if pending_entries:
        for chunk in _chunk_by_size(pending_entries, MAX_BATCH_REQUEST_BYTES):
            chunk_requests = [entry[1] for entry in chunk]
            chunk_garage_expected = {entry[0]: entry[2] for entry in chunk}
            chunk_bytes = sum(entry[3] for entry in chunk)
            batch = client.messages.batches.create(requests=chunk_requests)
            _append_checkpoint(batch.id, chunk_garage_expected)
            submitted_batches.append(
                {"batch_id": batch.id, "garage_expected_by_id": chunk_garage_expected}
            )
            print(
                f"submitted batch {batch.id} with {len(chunk_requests)} listings "
                f"(~{chunk_bytes / 1e6:.0f}MB estimated)"
            )

    if not submitted_batches:
        print("no listings had enough photos to score, and none already in flight")
        conn.close()
        return

    # Round-robin across all in-flight batches rather than fully polling one
    # to completion before even checking the next -- batches don't finish in
    # submission order (confirmed live, 2026-08-27: batch 1 of 9 was still
    # in_progress after 5 of the other 8 had already ended), so processing
    # strictly in list order can leave already-finished batches' results
    # sitting unprocessed while the script waits on an unrelated straggler.
    pending_batches = list(submitted_batches)
    while pending_batches:
        still_pending = []
        for batch_entry in pending_batches:
            batch_id = batch_entry["batch_id"]
            batch = client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                print(f"batch {batch_id}: ended, processing results")
                _process_batch_results(
                    client, conn, batch_id, batch_entry["garage_expected_by_id"]
                )
            else:
                still_pending.append(batch_entry)
        pending_batches = still_pending
        if pending_batches:
            print(
                f"{len(pending_batches)} batch(es) still processing, "
                f"waiting {POLL_INTERVAL_SECONDS}s"
            )
            time.sleep(POLL_INTERVAL_SECONDS)

    _clear_checkpoint()
    conn.close()


if __name__ == "__main__":
    main()

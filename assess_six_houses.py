"""One-off experiment: has Claude independently assess the six houses Ben and
Megan toured in person, from listing photos alone, so their real verdicts
(docs/house-tour-feedback.md) can be compared against a photo-only read.

Deliberately NOT wired into src/vision.py or the visual_scores table -- those
don't exist yet (see docs/superpowers/plans/2026-08-15-photo-scoring.md). This
is a prototype to validate the room-by-room rubric and prompting approach
against real photos and real money before that plan gets built for real.

Reads listing URLs out of docs/house-tour-feedback.md's "Listing Url"
fields, so it naturally picks up new houses as Ben fills the doc in. Safe to
re-run: skips any listing_id already present in the output file.
"""

import base64
import json
import random
import re
import time
from pathlib import Path

import anthropic
from dotenv import dotenv_values
from playwright.sync_api import Page

from src.auth import launch_authenticated_page
from src.config import load_config
from src.db import get_connection, upsert_listing
from src.models import Listing
from src.photos import download_photos
from src.scraper import scrape_listing
from src.store import save_listing

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
STORE_DIR = DATA_DIR / "listings"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
DB_PATH = DATA_DIR / "listings.db"
LOGIN_URL = "https://www.compass.com/login/"

FEEDBACK_PATH = Path("docs/house-tour-feedback.md")
OUTPUT_PATH = Path("docs/claude-six-house-assessment.md")

MODEL = "claude-sonnet-5"
# Sonnet 5 pricing, for the running cost log printed after each listing --
# not billed by this script, just a live estimate against the $5 balance.
INPUT_COST_PER_MTOK = 2.00
OUTPUT_COST_PER_MTOK = 10.00

PHOTO_JITTER_MIN_SECONDS = 0.15
PHOTO_JITTER_MAX_SECONDS = 0.5

_LISTING_URL_RE = re.compile(r"^-\s+\*\*Listing Url:\*\*\s*(https?://\S+)", re.MULTILINE)


def _photo_jitter() -> None:
    time.sleep(random.uniform(PHOTO_JITTER_MIN_SECONDS, PHOTO_JITTER_MAX_SECONDS))


def _build_fetch_bytes(page: Page):
    def fetch_bytes(url: str) -> bytes:
        response = page.request.get(url)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} fetching {url}")
        return response.body()

    return fetch_bytes


def parse_feedback_urls(path: Path) -> list[str]:
    """Every non-empty '**Listing Url:**' value in the feedback doc, in
    the order they appear."""
    if not path.exists():
        return []
    return _LISTING_URL_RE.findall(path.read_text())


def already_assessed_ids(path: Path) -> set[str]:
    """listing_ids that already have a section in the output file, so a
    re-run only spends money on genuinely new listings."""
    if not path.exists():
        return set()
    return set(re.findall(r"<!-- listing_id: (\S+) -->", path.read_text()))


# --- Assessment schema: same room-by-room rubric as
# docs/superpowers/specs/2026-08-15-photo-scoring-design.md, extended with an
# overall verdict/reasoning so it's directly comparable to Ben's "top reasons
# for the verdict" section. No min/max on numbers and additionalProperties:
# false everywhere, matching the Claude structured-outputs constraints the
# design spec already notes.

_ROOM = {
    "type": "object",
    "properties": {
        "status": {"enum": ["present", "omitted"]},
        "score": {"type": ["integer", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["status", "score", "notes"],
    "additionalProperties": False,
}

_BASEMENT = {
    "type": "object",
    "properties": {
        "status": {"enum": ["present", "omitted", "not_applicable"]},
        "score": {"type": ["integer", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["status", "score", "notes"],
    "additionalProperties": False,
}

_GARAGE = {
    "type": "object",
    "properties": {
        "status": {"enum": ["present", "omitted"]},
        "score": {"type": ["integer", "null"]},
        "notes": {"type": "string"},
        # Informational only -- never scored, just a formal note (matches
        # layout_plan's pattern). Ben flagged an unattached garage as a real
        # dealbreaker (960 E 9th Ave) that a bare condition score can't
        # distinguish from an attached one.
        "attached": {"type": ["boolean", "null"]},
    },
    "required": ["status", "score", "notes", "attached"],
    "additionalProperties": False,
}

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "kitchen": _ROOM,
        "bathrooms": _ROOM,
        "living_space": _ROOM,
        "basement": _BASEMENT,
        "garage": _GARAGE,
        "staging_flags": {
            "type": "object",
            "properties": {
                "watermarked_staging_detected": {"type": "boolean"},
                "suspected_unwatermarked_staging": {"type": "boolean"},
                "notes": {"type": "string"},
            },
            "required": [
                "watermarked_staging_detected", "suspected_unwatermarked_staging", "notes",
            ],
            "additionalProperties": False,
        },
        "backyard": {
            "type": "object",
            "properties": {
                "present": {"type": "boolean"},
                "tree_coverage": {"type": ["integer", "null"]},
                "hosting_suitability": {"type": ["integer", "null"]},
                "notes": {"type": "string"},
            },
            "required": ["present", "tree_coverage", "hosting_suitability", "notes"],
            "additionalProperties": False,
        },
        "layout_plan": {
            "type": "object",
            "properties": {
                "present": {"type": "boolean"},
                "clarity_score": {"type": ["integer", "null"]},
                "notes": {"type": "string"},
            },
            "required": ["present", "clarity_score", "notes"],
            "additionalProperties": False,
        },
        "gut_reaction": {"type": "string"},
        "overall_verdict": {"enum": ["yes", "no"]},
        "reasoning": {"type": "string"},
        "notable_photo_observations": {"type": "string"},
    },
    "required": [
        "kitchen", "bathrooms", "living_space", "basement", "garage",
        "staging_flags", "backyard", "layout_plan", "gut_reaction",
        "overall_verdict", "reasoning", "notable_photo_observations",
    ],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "You are assessing a real estate listing from its photos alone -- you have "
    "not toured it in person. Ignore any aerial/drone photos entirely -- whether "
    "they show this property or the surrounding neighborhood, they don't reliably "
    "show the home itself and shouldn't factor into any score, note, or "
    "observation below. For each room category (kitchen, bathrooms, living_space, "
    'garage), report status "present" with a 0-10 condition/quality score (10 = '
    'updated and move-in ready, 0 = dated or poorly maintained) if the photos '
    'clearly show it, or "omitted" if the room plausibly exists but no photo '
    'shows it. For basement, also allow "not_applicable" if the home clearly has '
    "no basement. Give each room a short one-sentence note explaining the score. "
    "For garage, also report whether it appears attached to the main house "
    "structure or a separate/detached building, when determinable from exterior "
    "photos (null if you can't tell). For the backyard, report whether it's "
    "shown, and if so rate tree/shade coverage and hosting/entertaining "
    "suitability, each 0-10, with a short note. Separately, report whether any "
    "photo is a floor-plan/layout graphic, and if so rate how legible and useful "
    "it is, 0-10.\n\n"
    "Staging: check every photo for a literal 'Virtual Staged' or 'Virtually "
    "Staged' watermark/caption burned into the image, and set "
    "watermarked_staging_detected accordingly. Separately, even where there's no "
    "watermark, look for visual signs a photo may still be virtually staged or "
    "unrealistically over-staged (furniture that looks slightly off in scale, "
    "perspective, or shadow direction; unnaturally crisp/rendered textures; "
    "empty-looking rooms with suspiciously perfect furniture) and set "
    "suspected_unwatermarked_staging accordingly. If either flag is true, treat "
    "that as a reason for lower confidence in the affected room(s)' scores -- "
    "note it explicitly and let it pull those scores toward the middle rather "
    "than taking staged photos at face value.\n\n"
    "Then give a one-sentence gut_reaction, an overall_verdict (yes/no -- would "
    "you seriously consider living here, based purely on these photos and the "
    "listing details below), 2-4 sentences of reasoning for that verdict, and "
    "any notable_photo_observations that don't fit the categories above (natural "
    "light, clutter, apparent age or wear, anything else you noticed)."
)


def assess_listing(client: anthropic.Anthropic, listing: Listing, photo_paths: list[Path]) -> dict:
    content = []
    for path in photo_paths:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(path.read_bytes()).decode("utf-8"),
            },
        })
    listing_context = (
        f"Address: {listing.address}, {listing.city}, {listing.state} {listing.zip_code}\n"
        f"Price: {listing.price}\n"
        f"Beds: {listing.beds}, Baths: {listing.baths}, Sqft: {listing.sqft}, "
        f"Lot sqft: {listing.lot_sqft}, Parking spaces: {listing.parking_spaces}, "
        f"Year built: {listing.year_built}\n"
        f"Description: {listing.description}\n"
        f"Amenities: {', '.join(listing.amenities)}"
    )
    content.append({"type": "text", "text": f"{listing_context}\n\n{INSTRUCTIONS}"})

    response = client.messages.create(
        model=MODEL,
        max_tokens=2560,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA}},
    )

    cost = (
        response.usage.input_tokens * INPUT_COST_PER_MTOK
        + response.usage.output_tokens * OUTPUT_COST_PER_MTOK
    ) / 1_000_000
    print(
        f"  usage: {response.usage.input_tokens} in / {response.usage.output_tokens} out "
        f"(~${cost:.4f})"
    )

    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def render_section(listing: Listing, listing_url: str, assessment: dict) -> str:
    def room_row(name: str, room: dict) -> str:
        return f"| {name} | {room['notes']} | {room['score'] if room['score'] is not None else 'N/A'} |"

    backyard = assessment["backyard"]
    layout = assessment["layout_plan"]
    garage = assessment["garage"]
    staging = assessment["staging_flags"]

    garage_type = (
        "attached" if garage["attached"] is True
        else "detached" if garage["attached"] is False
        else "unknown"
    )
    staging_note = (
        f"Watermarked staging detected: {staging['watermarked_staging_detected']}. "
        f"Suspected unwatermarked staging: {staging['suspected_unwatermarked_staging']}. "
        f"{staging['notes']}"
    )

    return f"""
## {listing.address}

<!-- listing_id: {listing.listing_id} -->

- **Listing Url:** {listing_url}
- **Price:** {listing.price}
- **Claude's verdict:** {assessment['overall_verdict'].upper()}

### Gut reaction
{assessment['gut_reaction']}

### Reasoning
{assessment['reasoning']}

### Room-by-room condition

| Room | Notes | Score (0-10) |
|---|---|---|
{room_row("Kitchen", assessment["kitchen"])}
{room_row("Bathrooms", assessment["bathrooms"])}
{room_row("Living space", assessment["living_space"])}
{room_row("Basement", assessment["basement"])}
{room_row("Garage", garage)}

**Garage type (formal note, not scored):** {garage_type}

### Outdoor / backyard
{backyard['notes']} (tree coverage: {backyard['tree_coverage']}, hosting suitability: {backyard['hosting_suitability']})

### Layout / floor plan
{layout['notes']} (present: {layout['present']}, clarity: {layout['clarity_score']})

### Staging
{staging_note}

### Notable photo observations
{assessment['notable_photo_observations']}

---
"""


def main() -> None:
    urls = parse_feedback_urls(FEEDBACK_PATH)
    if not urls:
        print(f"No listing URLs found in {FEEDBACK_PATH} yet.")
        return

    done_ids = already_assessed_ids(OUTPUT_PATH)

    env = dotenv_values(".env")
    if not env.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set in .env -- add it before running this script.")
        return

    config = load_config(env)
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    db_conn = get_connection(DB_PATH)

    if not OUTPUT_PATH.exists():
        OUTPUT_PATH.write_text(
            "# Claude's Six-House Assessment\n\n"
            "Independent per-listing assessments from photos alone, for comparison "
            "against `docs/house-tour-feedback.md`'s in-person notes. Generated by "
            "`assess_six_houses.py`.\n"
        )

    total_cost = 0.0
    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        for url in urls:
            try:
                listing = scrape_listing(page, url)
            except Exception as exc:
                print(f"skip (failed to scrape {url}): {exc}")
                continue

            if listing.listing_id in done_ids:
                print(f"skip (already assessed): {listing.address}")
                continue

            print(f"assessing: {listing.address}")
            upsert_listing(db_conn, listing, is_pinned=True)
            save_listing(STORE_DIR, listing)
            download_photos(
                listing.photo_urls,
                PHOTOS_DIR / listing.listing_id,
                _build_fetch_bytes(page),
                sleep_fn=_photo_jitter,
            )
            photo_paths = sorted((PHOTOS_DIR / listing.listing_id).glob("*.jpg"))
            if not photo_paths:
                print(f"skip (no photos downloaded): {listing.address}")
                continue

            try:
                assessment = assess_listing(client, listing, photo_paths)
            except Exception as exc:
                print(f"skip (assessment failed for {listing.address}): {exc}")
                continue

            with OUTPUT_PATH.open("a") as f:
                f.write(render_section(listing, url, assessment))
            print(f"  -> {assessment['overall_verdict'].upper()}: {assessment['gut_reaction']}")

    db_conn.close()
    print(f"\nWrote assessments to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

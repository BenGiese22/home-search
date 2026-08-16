# Photo Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every listing's condition and outdoor/hosting appeal from its actual
downloaded photos via Claude's vision + structured outputs, replacing the v1
keyword-only placeholders in the same weight slots.

**Architecture:** A new pure module (`src/vision.py`) holds the room-by-room rubric,
the JSON schema, and response parsing — no network calls, same split as
`src/commute.py`. A new `visual_scores` SQLite table caches results per listing, keyed
so a rerun only pays for listings it hasn't scored yet. A new top-level orchestration
script (`score_photos.py`) does the real work: builds one Message Batch request per
eligible listing (all its photos + a fixed instruction + the JSON schema), submits it,
polls, and upserts results. `src/scoring.py` and `score.py` (both from the v1 plan)
get small, additive edits so a visual score — when present — replaces the keyword
component of `condition_score`/`outdoor_score`, and falls back to the v1 behavior
when absent.

**Tech Stack:** Python 3.12, the official `anthropic` Python SDK (new dependency),
stdlib `sqlite3`/`json`/`base64`. No new test infrastructure.

**Spec:** `docs/superpowers/specs/2026-08-15-photo-scoring-design.md`

## Global Constraints

- **Dependency on the v1 baseline-scoring plan.** Tasks 1&ndash;4 below (`src/vision.py`,
  `src/photos.py`, the `visual_scores` table) are independent and can run any time.
  Tasks 5&ndash;7 modify `src/scoring.py` and `score.py`, which don't exist until the v1
  plan (`docs/superpowers/plans/2026-08-14-baseline-scoring.md`) is fully executed and
  merged. Do not start Task 5 until `src/scoring.py` exists with `score_condition`,
  `score_outdoor`, `score_listing`, `ScoreResult`, and `CollectionStats` as that plan
  defines them.
- No new `.env` variables. The `anthropic` SDK resolves `ANTHROPIC_API_KEY` (or an
  `ant auth login` profile) automatically — nothing project-specific to configure.
- New dependency: add `anthropic==0.116.0` to `requirements.txt` (Task 6).
- Named constants, not magic numbers: `MIN_PHOTOS_FOR_VISION_SCORING = 5`,
  `MISSING_CATEGORY_SCORE = 20.0`, `ROOM_WEIGHT_KITCHEN = 0.35`,
  `ROOM_WEIGHT_BATHROOMS = 0.30`, `ROOM_WEIGHT_LIVING_SPACE = 0.20`,
  `ROOM_WEIGHT_BASEMENT = 0.10`, `ROOM_WEIGHT_GARAGE = 0.05`.
- Structured-output JSON schemas in this plan avoid unsupported constraints (no
  `minimum`/`maximum` on numbers) and set `additionalProperties: false` on every
  object, per the Claude API's structured-outputs limitations.
- No live network calls in any test. `score_photos.py` is untested orchestration,
  matching `scrape.py`/`check.py`/`compute_commutes.py`/`score.py` precedent &mdash;
  **and it must not be smoke-tested against the real Anthropic API during
  implementation**, since `ANTHROPIC_API_KEY` billing isn't set up yet and each run
  costs real money. Verify only that it imports cleanly and is syntactically correct.
- Test house style, matching `tests/test_db.py`/`tests/test_scoring.py`: module-level
  fixtures, `tmp_path` for anything touching SQLite, no test classes.

---

## File Structure

- **Create `src/vision.py`** — room-by-room rubric constants, the JSON schema for
  structured output, image-encoding helper, and response parsing. Pure functions.
- **Modify `src/photos.py`** — add `count_downloaded_photos()`, used to decide whether
  a listing has enough photos to score.
- **Modify `src/db.py`** — add `visual_scores` table and its accessors, mirroring the
  `commute` table's pattern.
- **Modify `src/scoring.py`** (v1 file) — `score_condition`, `score_outdoor`, and
  `score_listing` gain optional visual-score parameters; `OUTDOOR_KEYWORDS` gains
  trail/park terms.
- **Create `score_photos.py`** (top-level, mirrors `compute_commutes.py`) — the real
  Anthropic Batch API calls: build requests, submit, poll, parse, upsert.
- **Modify `score.py`** (v1 file) — looks up each listing's visual score and passes it
  into `score_listing()`.

---

### Task 1: `src/vision.py` — photo-count floor and image encoding

**Files:**
- Create: `src/vision.py`
- Test: `tests/test_vision.py`

**Interfaces:**
- Produces: `MIN_PHOTOS_FOR_VISION_SCORING = 5`
- Produces: `has_enough_photos(photo_count: int) -> bool`
- Produces: `build_image_content_blocks(photo_paths: list[Path], read_bytes: Callable[[Path], bytes]) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vision.py
import base64
from pathlib import Path

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_vision.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.vision'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/vision.py
import base64
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_vision.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/vision.py tests/test_vision.py
git commit -m "feat(vision): add photo-count floor and image encoding"
```

---

### Task 2: `src/vision.py` — rubric, schema, and response parsing

**Files:**
- Modify: `src/vision.py`
- Test: `tests/test_vision.py`

**Interfaces:**
- Produces: `MISSING_CATEGORY_SCORE = 20.0`, `ROOM_WEIGHT_KITCHEN = 0.35`,
  `ROOM_WEIGHT_BATHROOMS = 0.30`, `ROOM_WEIGHT_LIVING_SPACE = 0.20`,
  `ROOM_WEIGHT_BASEMENT = 0.10`, `ROOM_WEIGHT_GARAGE = 0.05`
- Produces: `VISUAL_SCORE_SCHEMA: dict` (JSON schema for `output_config.format`)
- Produces: `VisualScoreResult` dataclass (`condition_photo_score: float,
  outdoor_photo_score: float`)
- Produces: `parse_visual_response(response_json: dict, garage_expected: bool) -> VisualScoreResult`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_vision.py
from src.vision import VisualScoreResult, parse_visual_response

FULL_RESPONSE = {
    "kitchen": {"status": "present", "score": 8},
    "bathrooms": {"status": "present", "score": 6},
    "living_space": {"status": "present", "score": 7},
    "basement": {"status": "present", "score": 5},
    "garage": {"status": "present", "score": 4},
    "backyard": {"present": True, "tree_coverage": 8, "hosting_suitability": 6},
}


def test_parse_visual_response_weighted_average_all_present():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    # .35*80 + .30*60 + .20*70 + .10*50 + .05*40 = 67.0
    assert result.condition_photo_score == 67.0


def test_parse_visual_response_excludes_not_applicable_basement():
    response = {**FULL_RESPONSE, "basement": {"status": "not_applicable", "score": None}}

    result = parse_visual_response(response, garage_expected=True)

    # weights renormalized over kitchen/bathrooms/living_space/garage (.35+.30+.20+.05=0.90)
    # (.35*80 + .30*60 + .20*70 + .05*40) / 0.90
    assert result.condition_photo_score == pytest.approx(68.888888888888, rel=1e-9)


def test_parse_visual_response_excludes_garage_when_not_expected():
    response = {**FULL_RESPONSE, "garage": {"status": "present", "score": 9}}

    result = parse_visual_response(response, garage_expected=False)

    # weights renormalized over kitchen/bathrooms/living_space/basement (.35+.30+.20+.10=0.95)
    # (.35*80 + .30*60 + .20*70 + .10*50) / 0.95
    assert result.condition_photo_score == pytest.approx(68.42105263157895, rel=1e-9)


def test_parse_visual_response_omitted_room_scores_low_not_dropped():
    response = {**FULL_RESPONSE, "kitchen": {"status": "omitted", "score": None}}

    result = parse_visual_response(response, garage_expected=True)

    # .35*20 + .30*60 + .20*70 + .10*50 + .05*40 = 46.0
    assert result.condition_photo_score == 46.0


def test_parse_visual_response_backyard_present_averages_sub_attributes():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    assert result.outdoor_photo_score == 70.0


def test_parse_visual_response_backyard_absent_scores_missing_category():
    response = {**FULL_RESPONSE, "backyard": {"present": False, "tree_coverage": None, "hosting_suitability": None}}

    result = parse_visual_response(response, garage_expected=True)

    assert result.outdoor_photo_score == 20.0


def test_parse_visual_response_returns_visual_score_result():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    assert isinstance(result, VisualScoreResult)
```

Add `import pytest` to the top of `tests/test_vision.py` if not already present (needed
for `pytest.approx`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_vision.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_visual_response' from 'src.vision'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/vision.py — add imports at top
from dataclasses import dataclass
```

```python
# src/vision.py — append at end of file
MISSING_CATEGORY_SCORE = 20.0

ROOM_WEIGHT_KITCHEN = 0.35
ROOM_WEIGHT_BATHROOMS = 0.30
ROOM_WEIGHT_LIVING_SPACE = 0.20
ROOM_WEIGHT_BASEMENT = 0.10
ROOM_WEIGHT_GARAGE = 0.05

_PRESENT_OMITTED_ROOM = {
    "type": "object",
    "properties": {
        "status": {"enum": ["present", "omitted"]},
        "score": {"type": ["integer", "null"]},
    },
    "required": ["status", "score"],
    "additionalProperties": False,
}

VISUAL_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "kitchen": _PRESENT_OMITTED_ROOM,
        "bathrooms": _PRESENT_OMITTED_ROOM,
        "living_space": _PRESENT_OMITTED_ROOM,
        "basement": {
            "type": "object",
            "properties": {
                "status": {"enum": ["present", "omitted", "not_applicable"]},
                "score": {"type": ["integer", "null"]},
            },
            "required": ["status", "score"],
            "additionalProperties": False,
        },
        "garage": _PRESENT_OMITTED_ROOM,
        "backyard": {
            "type": "object",
            "properties": {
                "present": {"type": "boolean"},
                "tree_coverage": {"type": ["integer", "null"]},
                "hosting_suitability": {"type": ["integer", "null"]},
            },
            "required": ["present", "tree_coverage", "hosting_suitability"],
            "additionalProperties": False,
        },
    },
    "required": ["kitchen", "bathrooms", "living_space", "basement", "garage", "backyard"],
    "additionalProperties": False,
}


@dataclass
class VisualScoreResult:
    condition_photo_score: float
    outdoor_photo_score: float


def _room_contribution(room: dict) -> float | None:
    """Returns a room's 0-100 contribution to the condition average, or None
    when it should be excluded entirely (not_applicable) rather than scored."""
    status = room["status"]
    if status == "not_applicable":
        return None
    if status == "omitted":
        return MISSING_CATEGORY_SCORE
    return room["score"] * 10.0


def parse_visual_response(response_json: dict, garage_expected: bool) -> VisualScoreResult:
    weighted = [
        (ROOM_WEIGHT_KITCHEN, _room_contribution(response_json["kitchen"])),
        (ROOM_WEIGHT_BATHROOMS, _room_contribution(response_json["bathrooms"])),
        (ROOM_WEIGHT_LIVING_SPACE, _room_contribution(response_json["living_space"])),
        (ROOM_WEIGHT_BASEMENT, _room_contribution(response_json["basement"])),
    ]
    if garage_expected:
        weighted.append((ROOM_WEIGHT_GARAGE, _room_contribution(response_json["garage"])))

    applicable = [(weight, score) for weight, score in weighted if score is not None]
    total_weight = sum(weight for weight, _ in applicable)
    condition_photo_score = sum(weight * score for weight, score in applicable) / total_weight

    backyard = response_json["backyard"]
    if backyard["present"]:
        outdoor_photo_score = (
            backyard["tree_coverage"] * 10.0 + backyard["hosting_suitability"] * 10.0
        ) / 2.0
    else:
        outdoor_photo_score = MISSING_CATEGORY_SCORE

    return VisualScoreResult(
        condition_photo_score=condition_photo_score,
        outdoor_photo_score=outdoor_photo_score,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_vision.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/vision.py tests/test_vision.py
git commit -m "feat(vision): add room-by-room rubric and response parsing"
```

---

### Task 3: `src/photos.py` — count downloaded photos

**Files:**
- Modify: `src/photos.py`
- Test: `tests/test_photos.py`

**Interfaces:**
- Produces: `count_downloaded_photos(photos_dir: Path, listing_id: str) -> int`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_photos.py
from src.photos import count_downloaded_photos


def test_count_downloaded_photos_counts_jpg_files(tmp_path: Path):
    listing_dir = tmp_path / "abc123"
    listing_dir.mkdir()
    (listing_dir / "01.jpg").write_bytes(b"x")
    (listing_dir / "02.jpg").write_bytes(b"x")

    assert count_downloaded_photos(tmp_path, "abc123") == 2


def test_count_downloaded_photos_returns_zero_when_dir_missing(tmp_path: Path):
    assert count_downloaded_photos(tmp_path, "nope") == 0


def test_count_downloaded_photos_ignores_non_jpg_files(tmp_path: Path):
    listing_dir = tmp_path / "abc123"
    listing_dir.mkdir()
    (listing_dir / "01.jpg").write_bytes(b"x")
    (listing_dir / "notes.txt").write_bytes(b"x")

    assert count_downloaded_photos(tmp_path, "abc123") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_photos.py -v`
Expected: FAIL — `ImportError: cannot import name 'count_downloaded_photos' from 'src.photos'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/photos.py — append at end of file
def count_downloaded_photos(photos_dir: Path, listing_id: str) -> int:
    """Counts already-downloaded photos for a listing, used to decide whether
    there's enough to run vision scoring on."""
    listing_dir = photos_dir / listing_id
    if not listing_dir.exists():
        return 0
    return len(list(listing_dir.glob("*.jpg")))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_photos.py -v`
Expected: PASS (all tests, including the pre-existing `download_photos` tests)

- [ ] **Step 5: Commit**

```bash
git add src/photos.py tests/test_photos.py
git commit -m "feat(photos): add downloaded-photo counter"
```

---

### Task 4: `src/db.py` — visual_scores table

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `VisualScoreResult` from `src/vision.py` (Task 2)
- Produces: `upsert_visual_score(conn: sqlite3.Connection, listing_id: str, result: VisualScoreResult | None, raw_response: str | None = None) -> None`
- Produces: `get_visual_score(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None`
- Produces: `get_listing_ids_missing_visual_score(conn: sqlite3.Connection) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_db.py
from src.vision import VisualScoreResult
from src.db import get_listing_ids_missing_visual_score, get_visual_score, upsert_visual_score

VISUAL_SCORE_SAMPLE = VisualScoreResult(condition_photo_score=67.0, outdoor_photo_score=70.0)


def test_upsert_visual_score_then_get_visual_score(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE, raw_response='{"kitchen": {}}')

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] == 67.0
    assert row["outdoor_photo_score"] == 70.0
    assert row["photo_score_unavailable"] == 0
    assert row["raw_response"] == '{"kitchen": {}}'


def test_upsert_visual_score_none_marks_unavailable(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(conn, "abc123", None)

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] is None
    assert row["outdoor_photo_score"] is None
    assert row["photo_score_unavailable"] == 1


def test_get_visual_score_returns_none_when_absent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    assert get_visual_score(conn, "abc123") is None


def test_upsert_visual_score_is_idempotent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)

    updated = VisualScoreResult(condition_photo_score=50.0, outdoor_photo_score=50.0)
    upsert_visual_score(conn, "abc123", updated)

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] == 50.0


def test_get_listing_ids_missing_visual_score(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)

    assert get_listing_ids_missing_visual_score(conn) == ["other456"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'upsert_visual_score' from 'src.db'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/db.py — add import at top
from src.vision import VisualScoreResult
```

```python
# src/db.py — add to _SCHEMA, before the closing triple-quote
CREATE TABLE IF NOT EXISTS visual_scores (
    listing_id TEXT PRIMARY KEY REFERENCES listings(listing_id),
    condition_photo_score REAL,
    outdoor_photo_score REAL,
    photo_score_unavailable INTEGER NOT NULL,
    raw_response TEXT,
    computed_at TEXT NOT NULL
);
```

```python
# src/db.py — append at end of file
def upsert_visual_score(
    conn: sqlite3.Connection,
    listing_id: str,
    result: VisualScoreResult | None,
    raw_response: str | None = None,
) -> None:
    """Insert or replace a listing's visual score. Pass result=None when the
    listing has too few photos or the vision call failed — scores are stored
    as NULL and photo_score_unavailable=True, signaling scoring.py to fall
    back to the v1 keyword-only computation."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO visual_scores (
                listing_id, condition_photo_score, outdoor_photo_score,
                photo_score_unavailable, raw_response, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                result.condition_photo_score if result else None,
                result.outdoor_photo_score if result else None,
                int(result is None),
                raw_response,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_visual_score(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM visual_scores WHERE listing_id = ?", (listing_id,)
    ).fetchone()


def get_listing_ids_missing_visual_score(conn: sqlite3.Connection) -> list[str]:
    """Listings with no visual_scores row yet — a rerun only pays the vision
    API cost for listings it hasn't already covered."""
    rows = conn.execute(
        """
        SELECT listing_id FROM listings
        WHERE listing_id NOT IN (SELECT listing_id FROM visual_scores)
        """
    ).fetchall()
    return [row["listing_id"] for row in rows]
```

`datetime`/`timezone` are already imported in `src/db.py` from the commute-table task;
do not re-import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (all `test_db.py` tests)

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): add visual_scores table and accessors"
```

---

### Task 5: `src/scoring.py` — accept optional visual scores; extend outdoor keywords

**Files:**
- Modify: `src/scoring.py` (created by the v1 baseline-scoring plan — read its current
  full contents before editing; this task adds optional parameters, it does not
  restructure anything)
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `score_condition`, `score_outdoor`, `score_listing`, `ScoreResult`,
  `CollectionStats`, `OUTDOOR_KEYWORDS`, `RENOVATION_KEYWORDS`, `NEUTRAL_SCORE`,
  `CONDITION_KEYWORD_WEIGHT`, `CONDITION_YEAR_WEIGHT`, `YEAR_BUILT_MIN`,
  `YEAR_BUILT_MAX`, `WEIGHT_COMMUTE`/`WEIGHT_SQFT`/`WEIGHT_CONDITION`/`WEIGHT_OUTDOOR`/`WEIGHT_PARKING`,
  `LISTING` test fixture — all exactly as defined by the v1 plan's Tasks 5&ndash;6
- Produces: `score_condition(description: str, amenities: list[str], year_built: int, visual_condition_score: float | None = None) -> float`
- Produces: `score_outdoor(description: str, amenities: list[str], visual_outdoor_score: float | None = None) -> float`
- Produces: `score_listing(listing: Listing, medtronic_minutes: float | None, denver_minutes: float | None, stats: CollectionStats, visual_condition_score: float | None = None, visual_outdoor_score: float | None = None) -> ScoreResult`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_scoring.py
import pytest


def test_score_condition_uses_visual_score_when_provided():
    with_visual = score_condition("no renovation keywords here", [], 1980, visual_condition_score=90.0)
    without_visual = score_condition("no renovation keywords here", [], 1980)

    assert with_visual > without_visual


def test_score_condition_visual_score_replaces_keyword_component_exactly():
    year_component = 0.2 * ((1980 - 1955) / (2005 - 1955) * 100.0)
    expected = 0.8 * 90.0 + year_component

    assert score_condition("irrelevant text with no keywords", [], 1980, visual_condition_score=90.0) == pytest.approx(expected)


def test_score_outdoor_uses_visual_score_when_provided():
    assert score_outdoor("no outdoor keywords here", [], visual_outdoor_score=75.0) == 75.0


def test_score_outdoor_falls_back_to_keywords_when_visual_score_absent():
    assert score_outdoor("Private yard with mature trees", []) == 100.0


def test_score_listing_passes_visual_scores_through():
    stats = CollectionStats(sqft_min=1000, sqft_max=3000, denver_minutes_min=10.0, denver_minutes_max=30.0)

    with_visual = score_listing(
        LISTING, medtronic_minutes=18.0, denver_minutes=15.0, stats=stats,
        visual_condition_score=90.0, visual_outdoor_score=80.0,
    )
    without_visual = score_listing(LISTING, medtronic_minutes=18.0, denver_minutes=15.0, stats=stats)

    assert with_visual.condition_score != without_visual.condition_score
    assert with_visual.outdoor_score != without_visual.outdoor_score
```

`LISTING`, `CollectionStats`, `score_condition`, `score_outdoor`, `score_listing` are
already imported/defined at the top of `tests/test_scoring.py` from the v1 plan — add
`import pytest` if it isn't already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scoring.py -v`
Expected: FAIL — `TypeError: score_condition() got an unexpected keyword argument 'visual_condition_score'`

- [ ] **Step 3: Write the minimal implementation**

Replace the existing `score_condition` function with:

```python
def score_condition(
    description: str,
    amenities: list[str],
    year_built: int,
    visual_condition_score: float | None = None,
) -> float:
    if visual_condition_score is not None:
        condition_component = visual_condition_score
    else:
        combined = f"{description} {' '.join(amenities)}"
        condition_component = 100.0 if _has_any_keyword(combined, RENOVATION_KEYWORDS) else 0.0
    if not year_built:
        year_score = NEUTRAL_SCORE
    else:
        normalized = (year_built - YEAR_BUILT_MIN) / (YEAR_BUILT_MAX - YEAR_BUILT_MIN)
        year_score = _clamp(normalized * 100.0)
    return CONDITION_KEYWORD_WEIGHT * condition_component + CONDITION_YEAR_WEIGHT * year_score
```

Replace the existing `score_outdoor` function with:

```python
def score_outdoor(
    description: str,
    amenities: list[str],
    visual_outdoor_score: float | None = None,
) -> float:
    if visual_outdoor_score is not None:
        return visual_outdoor_score
    combined = f"{description} {' '.join(amenities)}"
    return OUTDOOR_KEYWORD_HIT_SCORE if _has_any_keyword(combined, OUTDOOR_KEYWORDS) else OUTDOOR_NO_KEYWORD_SCORE
```

Replace the existing `score_listing` function with:

```python
def score_listing(
    listing: Listing,
    medtronic_minutes: float | None,
    denver_minutes: float | None,
    stats: CollectionStats,
    visual_condition_score: float | None = None,
    visual_outdoor_score: float | None = None,
) -> ScoreResult:
    commute_score = score_commute(
        medtronic_minutes, denver_minutes, stats.denver_minutes_min, stats.denver_minutes_max
    )
    sqft_score = score_sqft(listing.sqft, stats.sqft_min, stats.sqft_max)
    condition_score = score_condition(
        listing.description, listing.amenities, listing.year_built, visual_condition_score
    )
    outdoor_score = score_outdoor(listing.description, listing.amenities, visual_outdoor_score)
    parking_score = score_parking(listing.parking_spaces)
    composite = (
        WEIGHT_COMMUTE * commute_score
        + WEIGHT_SQFT * sqft_score
        + WEIGHT_CONDITION * condition_score
        + WEIGHT_OUTDOOR * outdoor_score
        + WEIGHT_PARKING * parking_score
    )
    return ScoreResult(
        commute_score=commute_score,
        sqft_score=sqft_score,
        condition_score=condition_score,
        outdoor_score=outdoor_score,
        parking_score=parking_score,
        composite=composite,
        passes_filters=passes_filters(listing.baths, listing.lot_sqft),
    )
```

Then extend the existing `OUTDOOR_KEYWORDS` list with trail/park terms:

```python
OUTDOOR_KEYWORDS = [
    "mature trees",
    "private yard",
    "backyard",
    "open floor plan",
    "entertaining",
    "outdoor living",
    "trail",
    "trails",
    "park",
    "greenbelt",
    "bike path",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scoring.py -v`
Expected: PASS (all tests, including the v1 tests already in the file)

- [ ] **Step 5: Commit**

```bash
git add src/scoring.py tests/test_scoring.py
git commit -m "feat(scoring): accept optional visual scores; add trail keywords"
```

---

### Task 6: `score_photos.py` — Batch API orchestration

**Files:**
- Create: `score_photos.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `has_enough_photos`, `build_image_content_blocks`, `parse_visual_response`,
  `VISUAL_SCORE_SCHEMA`, `MIN_PHOTOS_FOR_VISION_SCORING` from `src/vision.py`;
  `count_downloaded_photos` from `src/photos.py`; `get_connection`, `query_listings`,
  `get_listing_ids_missing_visual_score`, `upsert_visual_score` from `src/db.py`

No test file — untested orchestration, matching `scrape.py`/`check.py`/
`compute_commutes.py`/`score.py` precedent. **Do not run this script against the real
Anthropic API** — `ANTHROPIC_API_KEY` isn't configured yet and every real run costs
money. Verify only that it imports and compiles cleanly.

- [ ] **Step 1: Add the `anthropic` dependency**

Add this line to `requirements.txt`:

```
anthropic==0.116.0
```

Run: `pip install -r requirements.txt`
Expected: installs without error

- [ ] **Step 2: Write the script**

```python
# score_photos.py
import json
import time
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from src.db import (
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
POLL_INTERVAL_SECONDS = 60

INSTRUCTIONS = (
    "You are assessing the visual condition of a real estate listing from its "
    "photos. For each room category (kitchen, bathrooms, living_space, garage), "
    'report status "present" if the photos clearly show it, with a 0-10 '
    "condition/quality score (10 = updated and move-in ready, 0 = dated or "
    'poorly maintained), or "omitted" if the room plausibly exists but no '
    'photo shows it. For basement, also allow "not_applicable" if the home '
    "clearly has no basement. For the backyard, report whether it's shown, and "
    "if so rate tree/shade coverage and general hosting/entertaining "
    "suitability, each 0-10."
)


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def build_batch_request(listing_id: str, photo_paths: list[Path]) -> Request:
    content = build_image_content_blocks(photo_paths, read_bytes)
    content.append({"type": "text", "text": INSTRUCTIONS})
    return Request(
        custom_id=listing_id,
        params=MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=1024,
            output_config={"format": {"type": "json_schema", "schema": VISUAL_SCORE_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        ),
    )


def main() -> None:
    conn = get_connection(DB_PATH)
    listings_by_id = {row["listing_id"]: row for row in query_listings(conn)}
    missing_ids = get_listing_ids_missing_visual_score(conn)

    if not missing_ids:
        print("visual_scores table already covers every listing")
        conn.close()
        return

    requests = []
    garage_expected_by_id: dict[str, bool] = {}
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
        garage_expected_by_id[listing_id] = row["parking_spaces"] > 0
        requests.append(build_batch_request(listing_id, photo_paths))

    if not requests:
        print("no listings had enough photos to score")
        conn.close()
        return

    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)
    print(f"submitted batch {batch.id} with {len(requests)} listings")

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        print(f"batch status: {batch.processing_status}, waiting {POLL_INTERVAL_SECONDS}s")
        time.sleep(POLL_INTERVAL_SECONDS)

    for result in client.messages.batches.results(batch.id):
        listing_id = result.custom_id
        garage_expected = garage_expected_by_id[listing_id]
        if result.result.type != "succeeded":
            upsert_visual_score(conn, listing_id, None)
            print(f"{listing_id}: batch item {result.result.type}")
            continue
        try:
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
        print(
            f"{listing_id}: condition={visual_result.condition_photo_score:.0f} "
            f"outdoor={visual_result.outdoor_photo_score:.0f}"
        )

    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify the script imports cleanly (no live API call)**

Run: `python -c "import score_photos"`
Expected: no output, no traceback (a bare import runs no code inside `if __name__ ==
"__main__":`, so this cannot make a network call or spend money)

Also run: `python -m py_compile score_photos.py`
Expected: no output, no traceback

- [ ] **Step 4: Commit**

```bash
git add score_photos.py requirements.txt
git commit -m "feat(vision): add photo scoring batch orchestration script"
```

---

### Task 7: `score.py` — look up and pass through visual scores

**Files:**
- Modify: `score.py` (created by the v1 baseline-scoring plan — read its current full
  contents before editing)

**Interfaces:**
- Consumes: `get_visual_score` from `src/db.py` (Task 4); `score_listing`'s new
  `visual_condition_score`/`visual_outdoor_score` parameters (Task 5)

No test file — untested orchestration, matching v1's `score.py` precedent.

- [ ] **Step 1: Add the import**

Add `get_visual_score` to the existing `from src.db import (...)` line at the top of
`score.py`.

- [ ] **Step 2: Look up and pass the visual score in the scoring loop**

In `main()`'s per-listing loop, immediately before the call to `score_listing(...)`,
add:

```python
        visual_row = get_visual_score(conn, listing.listing_id)
        visual_condition_score = None
        visual_outdoor_score = None
        if visual_row is not None and not visual_row["photo_score_unavailable"]:
            visual_condition_score = visual_row["condition_photo_score"]
            visual_outdoor_score = visual_row["outdoor_photo_score"]
```

Then update the `score_listing(...)` call to pass them through:

```python
        result = score_listing(
            listing, medtronic_minutes, denver_minutes, stats,
            visual_condition_score=visual_condition_score,
            visual_outdoor_score=visual_outdoor_score,
        )
```

- [ ] **Step 3: Smoke-test against the real database**

Run: `python score.py`
Expected: same ranked report as before, no traceback. Since `visual_scores` is empty
until `score_photos.py` actually runs against the real API, every listing falls back to
`visual_condition_score=None`/`visual_outdoor_score=None` &mdash; the v1 keyword-only
behavior &mdash; confirming the fallback path works correctly.

- [ ] **Step 4: Commit**

```bash
git add score.py
git commit -m "feat(scoring): wire visual scores into the ranked report"
```

---

## Self-Review

**Spec coverage:**
- Room-by-room condition scoring (kitchen, bathrooms, living space, basement, garage),
  weighted and renormalized → Task 2 (`parse_visual_response`), boundary-tested for
  every `status` value. ✓
- Garage applicability driven by `parking_spaces`, not the model → Task 6
  (`garage_expected_by_id[listing_id] = row["parking_spaces"] > 0`), Task 2 (excluded
  from the average when `garage_expected=False`). ✓
- Basement `not_applicable` excluded from the average, not penalized → Task 2,
  `test_parse_visual_response_excludes_not_applicable_basement`. ✓
- Omitted room scores low, not dropped → Task 2, `MISSING_CATEGORY_SCORE`,
  `test_parse_visual_response_omitted_room_scores_low_not_dropped`. ✓
- Below-floor listings excluded from vision scoring entirely → Task 1
  (`has_enough_photos`), Task 6 (checked before building a request). ✓
- Cron-safe / idempotent (only scores listings without an existing row) → Task 4
  (`get_listing_ids_missing_visual_score`), Task 6 (the whole loop is built around it). ✓
- Trails/parks folded into the existing outdoor keyword slot, no new weighted category
  → Task 5, `OUTDOOR_KEYWORDS` extension. ✓
- One Claude request per listing, all photos batched, structured JSON output → Task 6,
  `build_batch_request`. ✓
- Message Batches API for the whole run → Task 6, `client.messages.batches.create` /
  `.retrieve` / `.results`. ✓
- Integration replaces the v1 placeholders in the same weight slots, falls back cleanly
  when absent → Task 5 (`score_condition`/`score_outdoor`/`score_listing`), Task 7
  (`score.py`'s lookup-or-`None` logic). ✓
- No new `.env` variables → confirmed; `anthropic.Anthropic()` resolves credentials on
  its own. ✓
- `score_photos.py` never live-tested during implementation → Task 6, Step 3 uses only
  a bare import and `py_compile`, never calls `main()`. ✓

**Placeholder scan:** No "TBD"/"handle appropriately"/"similar to Task N" language;
every step has literal code, including the full rewritten `score_condition`,
`score_outdoor`, and `score_listing` bodies in Task 5 so an implementer reading tasks
out of order never has to guess at unchanged parts.

**Type consistency:** `VisualScoreResult` (Task 2: `condition_photo_score,
outdoor_photo_score`) is read identically in Task 4's `upsert_visual_score` and Task
6's orchestration. `visual_condition_score`/`visual_outdoor_score` parameter names are
identical across Task 5's three function signatures and Task 7's call site. Task 6's
`garage_expected_by_id` dict is keyed by the same `listing_id` string used everywhere
else in this plan and the v1 plan (never `listing.listing_id` vs. a mismatched key).

---

Plan complete and saved to `docs/superpowers/plans/2026-08-15-photo-scoring.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?

# Photo Scoring Implementation Plan (v2 — calibration-informed)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Supersedes:** the original 2026-08-15 draft of this same file. Same core architecture (a pure `src/vision.py`, a `visual_scores` table, a `score_photos.py` Batch API orchestrator, small additive edits to `src/scoring.py`/`score.py`), but the schema now ports the staging-detection, garage-attachment, and aerial-exclusion fields validated live by `assess_six_houses.py`, and the cost estimate is grounded in real per-listing spend instead of a guess.

**Goal:** Score every listing's condition and outdoor/hosting appeal from its actual downloaded photos via Claude's vision + structured outputs, replacing the v1 keyword-only placeholders in the same weight slots — while also capturing (never scoring) staging risk, garage attachment, and floor-plan-graphic clarity as informational signals Ben can glance at.

**Architecture:**

```
listings + downloaded photos (data/photos/<listing_id>/*.jpg)
      +
score_photos.py (Batch API orchestration, new)
      ↓
src/vision.py (pure: photo-count floor, image encoding, rubric, schema, parsing)
      ↓
visual_scores table (src/db.py, new)
      ↓
src/scoring.py — score_condition()/score_outdoor() read visual_scores when present,
                  fall back to v1 keyword computation otherwise
      ↓
score.py — looks up visual score, passes through to score_listing()
```

**Tech Stack:** Python 3.12, `anthropic==0.116.0` (already in `requirements.txt` — no dependency work needed), stdlib `sqlite3`/`json`/`base64`. No new test infrastructure.

**Specs:** `docs/superpowers/specs/2026-08-15-photo-scoring-design.md` (architecture baseline), `docs/house-tour-calibration-findings.md` (what changed and why), `assess_six_houses.py` (validated schema/prompt to port).

## Global Constraints

- **Dependency on `src/scoring.py`'s current shape.** `src/scoring.py` already exists (unlike when the original plan was written) and already has `score_room_count`/`WEIGHT_ROOM_COUNT`, tuned `RENOVATION_KEYWORDS`/`OUTDOOR_KEYWORDS`, and softened no-match fallback scores (`CONDITION_NO_KEYWORD_SCORE = 40.0`, `OUTDOOR_NO_KEYWORD_SCORE = 40.0`). **Task 5 below must not touch any of that** — no keyword-list edits, no fallback-score edits, no `room_count`/`parking` changes. It adds exactly two optional parameters to `score_condition`/`score_outdoor`/`score_listing` and nothing else.
- Tasks 1–4 (`src/vision.py`, `src/photos.py`, `visual_scores` table) are independent of Task 5 and can run any time. Task 5 depends on `src/scoring.py` in its current form (already true today). Task 6 depends on Tasks 1–4. Task 7 depends on Tasks 4–6.
- No new `.env` variables and no `requirements.txt` change — `anthropic==0.116.0` is already installed; `anthropic.Anthropic()` resolves `ANTHROPIC_API_KEY` on its own.
- Named constants, not magic numbers: `MIN_PHOTOS_FOR_VISION_SCORING = 5`, `MISSING_CATEGORY_SCORE = 20.0`, `ROOM_WEIGHT_KITCHEN = 0.35`, `ROOM_WEIGHT_BATHROOMS = 0.30`, `ROOM_WEIGHT_LIVING_SPACE = 0.20`, `ROOM_WEIGHT_BASEMENT = 0.10`, `ROOM_WEIGHT_GARAGE = 0.05`. These stay exactly as originally spec'd — recalibrating weights or `MISSING_CATEGORY_SCORE` is explicitly a v3 concern, not this pass.
- Structured-output JSON schemas avoid unsupported constraints (no `minimum`/`maximum` on numbers) and set `additionalProperties: false` on every object — matches `assess_six_houses.py`'s already-validated schema shape.
- **Aerial/drone photos are excluded via a prompt-level instruction only** (in `score_photos.py`'s `INSTRUCTIONS` string) — there is no schema field, no code-level filtering, and no attempt to detect/classify/route them anywhere in `src/vision.py`. All downloaded photos are still sent to the model; the model is told to disregard aerial/drone shots entirely when reasoning about every score, note, and observation.
- **Layout/vertical-circulation confusion is explicitly out of scope** — see "Out of scope" section below. `layout_plan` stays scoped to detecting a floor-plan/layout *graphic* photo and rating its own clarity; it never becomes a proxy for flow, circulation, or the split-level confusion the calibration findings flagged as the single biggest real rejection reason.
- **Staging flags and garage attachment are informational only, exactly like `layout_plan`** — never entering `condition_photo_score`, `outdoor_photo_score`, or the composite. Their only effect on the numeric scores is already baked in at the *prompt* level: the model itself is instructed to pull a room's own reported score toward the middle when it suspects staging, before that score is returned to us. `parse_visual_response` does no additional staging-aware math — it only extracts and stores the flags.
- No live network calls in any test. `score_photos.py` is untested orchestration, matching `scrape.py`/`check.py`/`compute_commutes.py`/`score.py` precedent — **it must not be smoke-tested against the real Anthropic API during implementation**, since a real run costs money. Verify only that it imports and compiles cleanly.
- Test house style, matching `tests/test_db.py`/`tests/test_scoring.py`: module-level fixtures, `tmp_path` for anything touching SQLite, no test classes.
- **A latent bug in the superseded plan is fixed here:** its `VisualScoreResult` dataclass had four required fields (no defaults), but its own DB idempotency test constructed one with only two kwargs — that would have raised `TypeError` the moment it ran. This plan gives every field beyond `condition_photo_score`/`outdoor_photo_score` a default, so terse construction in tests (and in `score_photos.py`'s failure paths) type-checks correctly.

---

## File Structure

- **Create `src/vision.py`** — room-by-room rubric constants, the full JSON schema for structured output (condition rooms, backyard, layout_plan, staging_flags, garage with `attached`), image-encoding helper, and response parsing. Pure functions, no network calls.
- **Modify `src/photos.py`** — add `count_downloaded_photos()`.
- **Modify `src/db.py`** — add `visual_scores` table (sized for the full schema, including staging/garage informational columns) and its accessors.
- **Modify `src/scoring.py`** — `score_condition`, `score_outdoor`, and `score_listing` gain optional visual-score parameters only. No other edits.
- **Create `score_photos.py`** (top-level, mirrors `compute_commutes.py`) — Batch API orchestration, ported prompt/schema from `assess_six_houses.py`.
- **Modify `score.py`** — looks up each listing's visual score and passes it into `score_listing()`.

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_vision.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.vision'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/vision.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_vision.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/vision.py tests/test_vision.py
git commit -m "feat(vision): add photo-count floor and image encoding"
```

---

### Task 2: `src/vision.py` — rubric, schema (incl. staging/garage/layout), and response parsing

This is the task that ports `assess_six_houses.py`'s validated schema into the real module. It deliberately **does not** port that prototype's `gut_reaction`/`overall_verdict`/`reasoning`/`notable_photo_observations` fields — those existed only so the prototype's output could be rendered into a markdown doc for Ben to compare against his own tour notes; the real pipeline has no such comparison step and `scoring.py` never reads free-text verdicts. Everything that *is* a rubric signal (room-by-room condition with per-room `notes`, backyard, `layout_plan`, `staging_flags`, garage `attached`) is ported field-for-field.

**Files:**
- Modify: `src/vision.py`
- Test: `tests/test_vision.py`

**Interfaces:**
- Produces: `MISSING_CATEGORY_SCORE = 20.0`, `ROOM_WEIGHT_KITCHEN = 0.35`, `ROOM_WEIGHT_BATHROOMS = 0.30`, `ROOM_WEIGHT_LIVING_SPACE = 0.20`, `ROOM_WEIGHT_BASEMENT = 0.10`, `ROOM_WEIGHT_GARAGE = 0.05`
- Produces: `VISUAL_SCORE_SCHEMA: dict`
- Produces: `VisualScoreResult` dataclass
- Produces: `parse_visual_response(response_json: dict, garage_expected: bool) -> VisualScoreResult`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_vision.py
from src.vision import VisualScoreResult, parse_visual_response

FULL_RESPONSE = {
    "kitchen": {"status": "present", "score": 8, "notes": "Updated cabinets, newer appliances."},
    "bathrooms": {"status": "present", "score": 6, "notes": "Original tile, dated but clean."},
    "living_space": {"status": "present", "score": 7, "notes": "Open and bright."},
    "basement": {"status": "present", "score": 5, "notes": "Finished but low ceilings."},
    "garage": {
        "status": "present", "score": 4, "notes": "Detached, smaller door.",
        "attached": False,
    },
    "staging_flags": {
        "watermarked_staging_detected": False,
        "suspected_unwatermarked_staging": False,
        "notes": "No staging concerns noticed.",
    },
    "backyard": {
        "present": True, "tree_coverage": 8, "hosting_suitability": 6,
        "notes": "Large shaded patio.",
    },
    "layout_plan": {"present": True, "clarity_score": 9, "notes": "Crisp labeled floor plan photo."},
}


def test_parse_visual_response_weighted_average_all_present():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    # .35*80 + .30*60 + .20*70 + .10*50 + .05*40 = 67.0
    assert result.condition_photo_score == 67.0


def test_parse_visual_response_excludes_not_applicable_basement():
    response = {**FULL_RESPONSE, "basement": {"status": "not_applicable", "score": None, "notes": "No basement."}}

    result = parse_visual_response(response, garage_expected=True)

    # weights renormalized over kitchen/bathrooms/living_space/garage (.35+.30+.20+.05=0.90)
    assert result.condition_photo_score == pytest.approx(68.888888888888, rel=1e-9)


def test_parse_visual_response_excludes_garage_when_not_expected():
    response = {**FULL_RESPONSE, "garage": {"status": "present", "score": 9, "notes": "n/a", "attached": None}}

    result = parse_visual_response(response, garage_expected=False)

    # weights renormalized over kitchen/bathrooms/living_space/basement (.35+.30+.20+.10=0.95)
    assert result.condition_photo_score == pytest.approx(68.42105263157895, rel=1e-9)


def test_parse_visual_response_omitted_room_scores_low_not_dropped():
    response = {**FULL_RESPONSE, "kitchen": {"status": "omitted", "score": None, "notes": "No kitchen photos."}}

    result = parse_visual_response(response, garage_expected=True)

    # .35*20 + .30*60 + .20*70 + .10*50 + .05*40 = 46.0
    assert result.condition_photo_score == 46.0


def test_parse_visual_response_backyard_present_averages_sub_attributes():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    assert result.outdoor_photo_score == 70.0


def test_parse_visual_response_backyard_absent_scores_missing_category():
    response = {
        **FULL_RESPONSE,
        "backyard": {"present": False, "tree_coverage": None, "hosting_suitability": None, "notes": "No yard shown."},
    }

    result = parse_visual_response(response, garage_expected=True)

    assert result.outdoor_photo_score == 20.0


def test_parse_visual_response_returns_visual_score_result():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    assert isinstance(result, VisualScoreResult)


def test_parse_visual_response_captures_layout_plan_when_present():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    assert result.has_layout_plan is True
    assert result.layout_plan_clarity_score == 9.0


def test_parse_visual_response_captures_layout_plan_when_absent():
    response = {**FULL_RESPONSE, "layout_plan": {"present": False, "clarity_score": None, "notes": ""}}

    result = parse_visual_response(response, garage_expected=True)

    assert result.has_layout_plan is False
    assert result.layout_plan_clarity_score is None


def test_parse_visual_response_layout_plan_does_not_affect_condition_or_outdoor():
    with_plan = parse_visual_response(FULL_RESPONSE, garage_expected=True)
    without_plan = parse_visual_response(
        {**FULL_RESPONSE, "layout_plan": {"present": False, "clarity_score": None, "notes": ""}},
        garage_expected=True,
    )

    assert with_plan.condition_photo_score == without_plan.condition_photo_score
    assert with_plan.outdoor_photo_score == without_plan.outdoor_photo_score


def test_parse_visual_response_captures_garage_attached_true():
    response = {**FULL_RESPONSE, "garage": {**FULL_RESPONSE["garage"], "attached": True}}

    result = parse_visual_response(response, garage_expected=True)

    assert result.garage_attached is True


def test_parse_visual_response_captures_garage_attached_false():
    result = parse_visual_response(FULL_RESPONSE, garage_expected=True)

    assert result.garage_attached is False


def test_parse_visual_response_captures_garage_attached_null_when_undeterminable():
    response = {**FULL_RESPONSE, "garage": {**FULL_RESPONSE["garage"], "attached": None}}

    result = parse_visual_response(response, garage_expected=True)

    assert result.garage_attached is None


def test_parse_visual_response_garage_attached_does_not_affect_condition_score():
    attached = parse_visual_response(
        {**FULL_RESPONSE, "garage": {**FULL_RESPONSE["garage"], "attached": True}}, garage_expected=True
    )
    detached = parse_visual_response(
        {**FULL_RESPONSE, "garage": {**FULL_RESPONSE["garage"], "attached": False}}, garage_expected=True
    )

    assert attached.condition_photo_score == detached.condition_photo_score


def test_parse_visual_response_captures_staging_flags():
    response = {
        **FULL_RESPONSE,
        "staging_flags": {
            "watermarked_staging_detected": True,
            "suspected_unwatermarked_staging": False,
            "notes": "\"Virtual Staged\" watermark visible on living room photo.",
        },
    }

    result = parse_visual_response(response, garage_expected=True)

    assert result.watermarked_staging_detected is True
    assert result.suspected_unwatermarked_staging is False
    assert result.staging_notes == "\"Virtual Staged\" watermark visible on living room photo."


def test_parse_visual_response_staging_flags_do_not_affect_scores():
    clean = parse_visual_response(FULL_RESPONSE, garage_expected=True)
    staged = parse_visual_response(
        {
            **FULL_RESPONSE,
            "staging_flags": {
                "watermarked_staging_detected": True,
                "suspected_unwatermarked_staging": True,
                "notes": "Kitchen furniture scale looks off.",
            },
        },
        garage_expected=True,
    )

    # parse_visual_response itself never re-derives a score from these flags --
    # any effect on a room's own score already happened inside the model's
    # response (per the prompt instruction in score_photos.py), not here.
    assert clean.condition_photo_score == staged.condition_photo_score
```

Add `import pytest` to the top of `tests/test_vision.py` if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_vision.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_visual_response' from 'src.vision'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/vision.py — append at end of file
MISSING_CATEGORY_SCORE = 20.0

ROOM_WEIGHT_KITCHEN = 0.35
ROOM_WEIGHT_BATHROOMS = 0.30
ROOM_WEIGHT_LIVING_SPACE = 0.20
ROOM_WEIGHT_BASEMENT = 0.10
ROOM_WEIGHT_GARAGE = 0.05

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
        # Informational only -- never scored, same treatment as layout_plan.
        # Ben flagged a detached garage as a real rejection reason (960 E 9th
        # Ave) that a bare condition score can't distinguish from an attached
        # one. null when the model can't tell from exterior photos.
        "attached": {"type": ["boolean", "null"]},
    },
    "required": ["status", "score", "notes", "attached"],
    "additionalProperties": False,
}

_STAGING_FLAGS = {
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
}

_BACKYARD = {
    "type": "object",
    "properties": {
        "present": {"type": "boolean"},
        "tree_coverage": {"type": ["integer", "null"]},
        "hosting_suitability": {"type": ["integer", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["present", "tree_coverage", "hosting_suitability", "notes"],
    "additionalProperties": False,
}

_LAYOUT_PLAN = {
    "type": "object",
    "properties": {
        "present": {"type": "boolean"},
        "clarity_score": {"type": ["integer", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["present", "clarity_score", "notes"],
    "additionalProperties": False,
}

VISUAL_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "kitchen": _ROOM,
        "bathrooms": _ROOM,
        "living_space": _ROOM,
        "basement": _BASEMENT,
        "garage": _GARAGE,
        "staging_flags": _STAGING_FLAGS,
        "backyard": _BACKYARD,
        "layout_plan": _LAYOUT_PLAN,
    },
    "required": [
        "kitchen", "bathrooms", "living_space", "basement", "garage",
        "staging_flags", "backyard", "layout_plan",
    ],
    "additionalProperties": False,
}


@dataclass
class VisualScoreResult:
    condition_photo_score: float
    outdoor_photo_score: float
    # Everything below is informational only -- never read by scoring.py or
    # folded into either score above. See
    # docs/house-tour-calibration-findings.md and
    # docs/superpowers/specs/2026-08-15-photo-scoring-design.md.
    has_layout_plan: bool = False
    layout_plan_clarity_score: float | None = None
    garage_attached: bool | None = None
    watermarked_staging_detected: bool = False
    suspected_unwatermarked_staging: bool = False
    staging_notes: str = ""


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

    layout_plan = response_json["layout_plan"]
    garage = response_json["garage"]
    staging = response_json["staging_flags"]

    return VisualScoreResult(
        condition_photo_score=condition_photo_score,
        outdoor_photo_score=outdoor_photo_score,
        has_layout_plan=layout_plan["present"],
        layout_plan_clarity_score=(
            float(layout_plan["clarity_score"]) if layout_plan["present"] else None
        ),
        garage_attached=garage["attached"],
        watermarked_staging_detected=staging["watermarked_staging_detected"],
        suspected_unwatermarked_staging=staging["suspected_unwatermarked_staging"],
        staging_notes=staging["notes"],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_vision.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/vision.py tests/test_vision.py
git commit -m "feat(vision): add room-by-room rubric, staging/garage/layout parsing"
```

---

### Task 3: `src/photos.py` — count downloaded photos

**Files:**
- Modify: `src/photos.py`
- Test: `tests/test_photos.py`

**Interfaces:**
- Produces: `count_downloaded_photos(photos_dir: Path, listing_id: str) -> int`

Verified against the current `src/photos.py` — it has only `download_photos`/`delete_photos` today, so this is a clean addition, no conflicts.

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

`Path` is already imported at the top of `tests/test_photos.py`.

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
Expected: PASS (all tests, including the pre-existing `download_photos`/`delete_photos` tests)

- [ ] **Step 5: Commit**

```bash
git add src/photos.py tests/test_photos.py
git commit -m "feat(photos): add downloaded-photo counter"
```

---

### Task 4: `src/db.py` — `visual_scores` table

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `VisualScoreResult` from `src/vision.py` (Task 2)
- Produces: `upsert_visual_score(conn: sqlite3.Connection, listing_id: str, result: VisualScoreResult | None, raw_response: str | None = None) -> None`
- Produces: `get_visual_score(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None`
- Produces: `get_listing_ids_missing_visual_score(conn: sqlite3.Connection) -> list[str]`

Confirmed against the current `src/db.py`: there is no `visual_scores` table today, so this is a brand-new `CREATE TABLE` with no migration/`ALTER TABLE` needed for pre-existing rows (unlike `has_incomplete_data`/`room_count_score`/`is_pinned`, which needed `init_db`'s patch-in-place logic because they were added to already-existing tables).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_db.py
from src.vision import VisualScoreResult
from src.db import get_listing_ids_missing_visual_score, get_visual_score, upsert_visual_score

VISUAL_SCORE_SAMPLE = VisualScoreResult(
    condition_photo_score=67.0,
    outdoor_photo_score=70.0,
    has_layout_plan=True,
    layout_plan_clarity_score=9.0,
    garage_attached=False,
    watermarked_staging_detected=False,
    suspected_unwatermarked_staging=False,
    staging_notes="No staging concerns.",
)


def test_upsert_visual_score_then_get_visual_score(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE, raw_response='{"kitchen": {}}')

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] == 67.0
    assert row["outdoor_photo_score"] == 70.0
    assert row["has_layout_plan"] == 1
    assert row["layout_plan_clarity_score"] == 9.0
    assert row["garage_attached"] == 0
    assert row["watermarked_staging_detected"] == 0
    assert row["suspected_unwatermarked_staging"] == 0
    assert row["staging_notes"] == "No staging concerns."
    assert row["photo_score_unavailable"] == 0
    assert row["raw_response"] == '{"kitchen": {}}'


def test_upsert_visual_score_stores_garage_attached_true_and_staging_flags(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    flagged = VisualScoreResult(
        condition_photo_score=50.0,
        outdoor_photo_score=50.0,
        garage_attached=True,
        watermarked_staging_detected=True,
        suspected_unwatermarked_staging=True,
        staging_notes="Watermark visible on 2 photos.",
    )
    upsert_visual_score(conn, "abc123", flagged)

    row = get_visual_score(conn, "abc123")
    assert row["garage_attached"] == 1
    assert row["watermarked_staging_detected"] == 1
    assert row["suspected_unwatermarked_staging"] == 1
    assert row["staging_notes"] == "Watermark visible on 2 photos."


def test_upsert_visual_score_stores_garage_attached_null_when_undeterminable(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(
        conn, "abc123",
        VisualScoreResult(condition_photo_score=50.0, outdoor_photo_score=50.0, garage_attached=None),
    )

    row = get_visual_score(conn, "abc123")
    assert row["garage_attached"] is None


def test_upsert_visual_score_none_marks_unavailable(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(conn, "abc123", None)

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] is None
    assert row["outdoor_photo_score"] is None
    assert row["has_layout_plan"] == 0
    assert row["layout_plan_clarity_score"] is None
    assert row["garage_attached"] is None
    assert row["watermarked_staging_detected"] == 0
    assert row["suspected_unwatermarked_staging"] == 0
    assert row["staging_notes"] is None
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
# src/db.py — add import at top, alongside the existing src.scoring import
from src.vision import VisualScoreResult
```

```python
# src/db.py — add to _SCHEMA, before the closing triple-quote
CREATE TABLE IF NOT EXISTS visual_scores (
    listing_id TEXT PRIMARY KEY REFERENCES listings(listing_id),
    condition_photo_score REAL,
    outdoor_photo_score REAL,
    has_layout_plan INTEGER NOT NULL DEFAULT 0,
    layout_plan_clarity_score REAL,
    garage_attached INTEGER,
    watermarked_staging_detected INTEGER NOT NULL DEFAULT 0,
    suspected_unwatermarked_staging INTEGER NOT NULL DEFAULT 0,
    staging_notes TEXT,
    photo_score_unavailable INTEGER NOT NULL,
    raw_response TEXT,
    computed_at TEXT NOT NULL
);
```

```python
# src/db.py — append at end of file
def _bool_or_none_to_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def upsert_visual_score(
    conn: sqlite3.Connection,
    listing_id: str,
    result: VisualScoreResult | None,
    raw_response: str | None = None,
) -> None:
    """Insert or replace a listing's visual score. Pass result=None when the
    listing has too few photos or the vision call failed -- scores are stored
    as NULL and photo_score_unavailable=True, signaling scoring.py to fall
    back to the v1 keyword-only computation. garage_attached/staging flags
    are informational only -- stored for Ben to glance at, never read by
    scoring.py."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO visual_scores (
                listing_id, condition_photo_score, outdoor_photo_score,
                has_layout_plan, layout_plan_clarity_score, garage_attached,
                watermarked_staging_detected, suspected_unwatermarked_staging,
                staging_notes, photo_score_unavailable, raw_response, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                result.condition_photo_score if result else None,
                result.outdoor_photo_score if result else None,
                int(result.has_layout_plan) if result else 0,
                result.layout_plan_clarity_score if result else None,
                _bool_or_none_to_int(result.garage_attached) if result else None,
                int(result.watermarked_staging_detected) if result else 0,
                int(result.suspected_unwatermarked_staging) if result else 0,
                result.staging_notes if result else None,
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
    """Listings with no visual_scores row yet -- a rerun only pays the vision
    API cost for listings it hasn't already covered."""
    rows = conn.execute(
        """
        SELECT listing_id FROM listings
        WHERE listing_id NOT IN (SELECT listing_id FROM visual_scores)
        """
    ).fetchall()
    return [row["listing_id"] for row in rows]
```

`datetime`/`timezone` are already imported in `src/db.py`; do not re-import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (all `test_db.py` tests)

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): add visual_scores table with staging/garage columns"
```

---

### Task 5: `src/scoring.py` — accept optional visual scores (additive only)

**Files:**
- Modify: `src/scoring.py` (already exists; this task adds two optional parameters to three functions and touches nothing else: `RENOVATION_KEYWORDS`, `OUTDOOR_KEYWORDS`, `CONDITION_NO_KEYWORD_SCORE`, `OUTDOOR_NO_KEYWORD_SCORE`, `score_room_count`, `score_parking`, `passes_filters`, `CollectionStats`, `ScoreResult.room_count_score`/`has_incomplete_data` all stay exactly as they are today)
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes (unchanged, exactly as currently defined): `CollectionStats`, `WEIGHT_COMMUTE`, `WEIGHT_CONDITION`, `WEIGHT_OUTDOOR`, `WEIGHT_PARKING`, `WEIGHT_ROOM_COUNT`, `WEIGHT_SQFT`, `CONDITION_KEYWORD_WEIGHT`, `CONDITION_YEAR_WEIGHT`, `YEAR_BUILT_MIN`, `YEAR_BUILT_MAX`, `NEUTRAL_SCORE`, `LISTING` test fixture
- Produces: `score_condition(description: str, amenities: list[str], year_built: int, visual_condition_score: float | None = None) -> float`
- Produces: `score_outdoor(description: str, amenities: list[str], visual_outdoor_score: float | None = None) -> float`
- Produces: `score_listing(listing: Listing, medtronic_minutes: float | None, denver_minutes: float | None, stats: CollectionStats, visual_condition_score: float | None = None, visual_outdoor_score: float | None = None) -> ScoreResult`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_scoring.py (add "import pytest" near the top if it
# isn't already there)
from src.scoring import CONDITION_KEYWORD_WEIGHT, CONDITION_YEAR_WEIGHT, YEAR_BUILT_MAX, YEAR_BUILT_MIN


def test_score_condition_uses_visual_score_when_provided():
    with_visual = score_condition("no renovation keywords here", [], 1980, visual_condition_score=90.0)
    without_visual = score_condition("no renovation keywords here", [], 1980)

    assert with_visual > without_visual


def test_score_condition_visual_score_replaces_keyword_component_exactly():
    year_score = (1980 - YEAR_BUILT_MIN) / (YEAR_BUILT_MAX - YEAR_BUILT_MIN) * 100.0
    expected = CONDITION_KEYWORD_WEIGHT * 90.0 + CONDITION_YEAR_WEIGHT * year_score

    result = score_condition("irrelevant text with no keywords", [], 1980, visual_condition_score=90.0)
    assert result == pytest.approx(expected)


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
    # everything else this task doesn't touch should be identical
    assert with_visual.room_count_score == without_visual.room_count_score
    assert with_visual.parking_score == without_visual.parking_score
    assert with_visual.has_incomplete_data == without_visual.has_incomplete_data
```

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
        condition_component = (
            CONDITION_KEYWORD_HIT_SCORE
            if _has_any_keyword(combined, RENOVATION_KEYWORDS)
            else CONDITION_NO_KEYWORD_SCORE
        )
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
    return (
        OUTDOOR_KEYWORD_HIT_SCORE
        if _has_any_keyword(combined, OUTDOOR_KEYWORDS)
        else OUTDOOR_NO_KEYWORD_SCORE
    )
```

Replace the existing `score_listing` function with (only the two new parameters and the two forwarded call sites change; `room_count_score`, `parking_score`, `has_incomplete_data`, and the composite formula are byte-for-byte what's in the file today):

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
    room_count_score = score_room_count(
        listing.beds, listing.baths, stats.room_count_min, stats.room_count_max
    )
    parking_score = score_parking(listing.parking_spaces)
    composite = (
        WEIGHT_COMMUTE * commute_score
        + WEIGHT_SQFT * sqft_score
        + WEIGHT_CONDITION * condition_score
        + WEIGHT_OUTDOOR * outdoor_score
        + WEIGHT_ROOM_COUNT * room_count_score
        + WEIGHT_PARKING * parking_score
    )
    has_incomplete_data = (
        medtronic_minutes is None
        or denver_minutes is None
        or not listing.sqft
        or not listing.year_built
        or not listing.beds
    )
    return ScoreResult(
        commute_score=commute_score,
        sqft_score=sqft_score,
        condition_score=condition_score,
        outdoor_score=outdoor_score,
        room_count_score=room_count_score,
        parking_score=parking_score,
        composite=composite,
        passes_filters=passes_filters(listing.baths, listing.lot_sqft),
        has_incomplete_data=has_incomplete_data,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scoring.py -v`
Expected: PASS (all tests, including every pre-existing test in the file — `RENOVATION_KEYWORDS`/`OUTDOOR_KEYWORDS`/fallback scores/`score_room_count`/`score_parking` are untouched)

- [ ] **Step 5: Commit**

```bash
git add src/scoring.py tests/test_scoring.py
git commit -m "feat(scoring): accept optional visual condition/outdoor scores"
```

---

### Task 6: `score_photos.py` — Batch API orchestration (ported prompt/schema)

**Files:**
- Create: `score_photos.py`

No `requirements.txt` change — `anthropic==0.116.0` is already installed.

**Interfaces:**
- Consumes: `has_enough_photos`, `build_image_content_blocks`, `parse_visual_response`, `VISUAL_SCORE_SCHEMA`, `MIN_PHOTOS_FOR_VISION_SCORING` from `src/vision.py`; `count_downloaded_photos` from `src/photos.py`; `get_connection`, `query_listings`, `get_amenities`, `get_listing_ids_missing_visual_score`, `upsert_visual_score` from `src/db.py`

No test file — untested orchestration, matching `scrape.py`/`check.py`/`compute_commutes.py`/`score.py` precedent. **Do not run this script against the real Anthropic API during implementation** — every real run costs money. Verify only that it imports and compiles cleanly.

- [ ] **Step 1: Write the script**

```python
# score_photos.py
import json
import time
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

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
        amenities = get_amenities(conn, listing_id)
        garage_expected_by_id[listing_id] = row["parking_spaces"] > 0
        requests.append(build_batch_request(listing_id, row, amenities, photo_paths))

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
        staging_flag = (
            " [STAGING FLAGGED]"
            if visual_result.watermarked_staging_detected or visual_result.suspected_unwatermarked_staging
            else ""
        )
        print(
            f"{listing_id}: condition={visual_result.condition_photo_score:.0f} "
            f"outdoor={visual_result.outdoor_photo_score:.0f}{staging_flag}"
        )

    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the script imports cleanly (no live API call)**

Run: `python -c "import score_photos"`
Expected: no output, no traceback

Also run: `python -m py_compile score_photos.py`
Expected: no output, no traceback

- [ ] **Step 3: Commit**

```bash
git add score_photos.py
git commit -m "feat(vision): add photo scoring batch orchestration script"
```

---

### Task 7: `score.py` — look up and pass through visual scores

**Files:**
- Modify: `score.py` (current file already handles `room_count_score`/`value_score`/`--sort-by-value`; this task only adds the visual-score lookup and passes it through)

**Interfaces:**
- Consumes: `get_visual_score` from `src/db.py` (Task 4); `score_listing`'s new `visual_condition_score`/`visual_outdoor_score` parameters (Task 5)

No test file — untested orchestration, matching current `score.py` precedent.

- [ ] **Step 1: Add the import**

Change the existing import line:

```python
from src.db import get_amenities, get_commute, get_connection, query_listings, upsert_score
```

to:

```python
from src.db import get_amenities, get_commute, get_connection, get_visual_score, query_listings, upsert_score
```

- [ ] **Step 2: Look up and pass the visual score in the scoring loop**

In `main()`, immediately before the `for listing in listings:` scoring loop's call to `score_listing(...)`, change:

```python
    ranked = []
    for listing in listings:
        commute = commute_by_id[listing.listing_id]
        medtronic_minutes = commute["medtronic_minutes"] if commute else None
        denver_minutes = commute["denver_minutes"] if commute else None
        result = score_listing(listing, medtronic_minutes, denver_minutes, stats)
        upsert_score(conn, listing.listing_id, result)
        value = value_score(result.composite, price_numeric_by_id[listing.listing_id])
        ranked.append((listing, result, value))
```

to:

```python
    ranked = []
    for listing in listings:
        commute = commute_by_id[listing.listing_id]
        medtronic_minutes = commute["medtronic_minutes"] if commute else None
        denver_minutes = commute["denver_minutes"] if commute else None

        visual_row = get_visual_score(conn, listing.listing_id)
        visual_condition_score = None
        visual_outdoor_score = None
        if visual_row is not None and not visual_row["photo_score_unavailable"]:
            visual_condition_score = visual_row["condition_photo_score"]
            visual_outdoor_score = visual_row["outdoor_photo_score"]

        result = score_listing(
            listing, medtronic_minutes, denver_minutes, stats,
            visual_condition_score=visual_condition_score,
            visual_outdoor_score=visual_outdoor_score,
        )
        upsert_score(conn, listing.listing_id, result)
        value = value_score(result.composite, price_numeric_by_id[listing.listing_id])
        ranked.append((listing, result, value))
```

- [ ] **Step 3: Smoke-test against the real database**

Run: `python score.py`
Expected: same ranked report as before, no traceback. Since `visual_scores` is empty until `score_photos.py` actually runs against the real API, every listing falls back to `visual_condition_score=None`/`visual_outdoor_score=None` — the v1 keyword-only behavior — confirming the fallback path works.

- [ ] **Step 4: Commit**

```bash
git add score.py
git commit -m "feat(scoring): wire visual scores into the ranked report"
```

---

## Out of scope (restated)

**Layout/vertical-circulation detection is explicitly NOT part of this plan**, even though the calibration findings identify it as the single most repeated real rejection reason (4 of 7 toured houses: disjointed split-level flow, "spiral staircase maze of 4 half floors," stairwell placement). This is a known, accepted blind spot for this pass. `layout_plan` in `src/vision.py` stays scoped exactly to detecting whether a floor-plan/layout *graphic* photo exists among a listing's photos and rating that graphic's own legibility/clarity (`clarity_score`) — it says nothing about, and is never used as a proxy for, how a home's rooms actually connect or flow. No new field, heuristic, or model instruction in this plan attempts to infer circulation quality from room photos, photo ordering, or anything else. Solving this is tracked separately (per the calibration findings doc) as a future rubric concern, not this implementation.

Also out of scope, per the same reasoning as the original design spec: no reweighting of `WEIGHT_*`, no new top-level scored category for staging/garage/lot-context/school-district, and no recalibration of `MISSING_CATEGORY_SCORE`/room weights — those remain v3 concerns pending real scored-listing review.

## Cost estimate (revised)

The original design spec's ballpark (~114 listings × ~20 photos, "$5–10 total") was a guess made before any real API calls existed. Real synchronous (non-batch) Sonnet 5 requests against real listings (the 7 toured houses, 23–50 photos each) have now run at **$0.11–$0.24 per listing** (~50,000–115,000 input tokens, ~700–2,400 output tokens, cost dominated by image tokens and scaling with photo count).

For the real ~117-listing collection:
- That 7-house sample averages ~33 photos/listing — higher than the original spec's ~20-photo assumption — so the realistic per-listing sync cost likely sits in the middle-to-upper part of the observed $0.11–$0.24 range for a typical listing, not just the low end.
- **Sync-equivalent total, 117 listings:** roughly $13–$28, with ~$20–22 as a reasonable central estimate given the observed photo-count average.
- **With the Batch API's 50% discount** (applies to both input and output tokens): roughly **$6.50–$14**, with **~$10–11** as the working estimate.
- Production's schema drops the prototype's `gut_reaction`/`overall_verdict`/`reasoning`/`notable_photo_observations` free-text fields, which trims some output-token cost — but output tokens are a small fraction of the total next to image input tokens, so this doesn't meaningfully change the range above.

This is meaningfully higher than the original spec's "$5–10" floor, but still cheap enough not to gate the decision to run `score_photos.py` for real. Worth confirming with `client.messages.count_tokens` against a handful of real listings before submitting the full batch, since the real collection's photo-count distribution may differ from this 7-house sample.

---

## Self-Review

**Spec coverage:**
- Room-by-room condition scoring, weighted and renormalized → Task 2 (`parse_visual_response`), boundary-tested for every `status` value. ✓
- Garage applicability driven by `parking_spaces`, not the model → Task 6, Task 2. ✓
- Garage `attached` captured as a structured, informational-only field → Task 2, Task 4, Task 6. ✓
- Staging flags captured, informational-only, with the score-softening effect happening at the *prompt* level → Task 2, Task 4, Task 6. ✓
- Aerial/drone exclusion as a prompt-level instruction only, no schema field → Task 6's `INSTRUCTIONS`. ✓
- Basement `not_applicable` excluded from the average, not penalized → Task 2. ✓
- Omitted room scores low, not dropped → Task 2, `MISSING_CATEGORY_SCORE`. ✓
- Below-floor listings excluded from vision scoring entirely → Task 1, Task 6. ✓
- Cron-safe / idempotent → Task 4, Task 6. ✓
- Floor-plan/layout photos detected and rated but excluded from the composite, and NOT used as a circulation proxy → Task 2, "Out of scope" section. ✓
- One Claude request per listing, all photos batched, structured JSON output, listing context ported from the validated prototype → Task 6. ✓
- Message Batches API for the whole run → Task 6. ✓
- `score_condition`/`score_outdoor`/`score_listing` match the CURRENT actual signatures and this plan adds only two optional parameters, touching nothing else. ✓
- Integration replaces v1 placeholders in the same weight slots, falls back cleanly when absent → Task 5, Task 7. ✓
- `score_photos.py` never live-tested during implementation → Task 6, Step 2. ✓
- Refined cost estimate grounded in real per-listing spend → "Cost estimate (revised)" section. ✓

**Type consistency:** `VisualScoreResult` (Task 2, 8 fields, 6 with defaults) is read identically in Task 4's `upsert_visual_score` and Task 6's orchestration. `visual_condition_score`/`visual_outdoor_score` parameter names are identical across Task 5's three function signatures and Task 7's call site. Task 6's `garage_expected_by_id` dict is keyed by the same `listing_id` string used everywhere else in this plan.

**Fixed from the superseded plan:** `VisualScoreResult` now has defaults on every field beyond the two required scores, so terse test/production construction actually type-checks — the prior draft plan's DB idempotency test would have raised `TypeError` had it been run.

---

### Critical Files for Implementation

- /home/bengi/code/home-search/src/vision.py
- /home/bengi/code/home-search/src/scoring.py
- /home/bengi/code/home-search/src/db.py
- /home/bengi/code/home-search/score_photos.py
- /home/bengi/code/home-search/assess_six_houses.py

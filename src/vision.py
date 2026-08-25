import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MIN_PHOTOS_FOR_VISION_SCORING = 5

MISSING_CATEGORY_SCORE = 20.0

ROOM_WEIGHT_KITCHEN = 0.35
ROOM_WEIGHT_BATHROOMS = 0.30
ROOM_WEIGHT_LIVING_SPACE = 0.20
ROOM_WEIGHT_BASEMENT = 0.10
ROOM_WEIGHT_GARAGE = 0.05


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

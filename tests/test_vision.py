import base64
from pathlib import Path

import pytest

from src.vision import VisualScoreResult, build_image_content_blocks, has_enough_photos, parse_visual_response


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

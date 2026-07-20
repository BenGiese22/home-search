import json
import pytest

from src.json_extract import extract_balanced_json, find_listing_dicts_in_html


def test_extract_balanced_json_simple():
    text = 'window.uc = {"a": 1, "b": 2};'
    result = extract_balanced_json(text, text.index("{"))
    assert result == '{"a": 1, "b": 2}'


def test_extract_balanced_json_nested():
    text = 'window.uc = {"a": {"b": {"c": 1}}, "d": 2};'
    result = extract_balanced_json(text, text.index("{"))
    assert result == '{"a": {"b": {"c": 1}}, "d": 2}'


def test_extract_balanced_json_brace_in_string():
    text = 'window.uc = {"a": "value with { and } braces", "b": 2};'
    result = extract_balanced_json(text, text.index("{"))
    assert result == '{"a": "value with { and } braces", "b": 2}'


def test_extract_balanced_json_escaped_quote_in_string():
    text = r'window.uc = {"a": "she said \"hi\""};'
    result = extract_balanced_json(text, text.index("{"))
    assert result == r'{"a": "she said \"hi\""}'


def test_extract_balanced_json_no_closing_brace_raises():
    text = 'window.uc = {"a": 1'
    with pytest.raises(ValueError):
        extract_balanced_json(text, text.index("{"))


MINIMAL_LISTING = {
    "location": {"prettyAddress": "123 Main St"},
    "price": {"formatted": "$500,000"},
    "media": [{"originalUrl": "https://example.com/1.jpg"}],
}


def test_find_listing_dicts_in_html_script_assignment():
    html = f"<html><body><script>window.uc = {json.dumps(MINIMAL_LISTING)};</script></body></html>"
    results = find_listing_dicts_in_html(html)
    assert len(results) == 1
    assert results[0]["location"]["prettyAddress"] == "123 Main St"


def test_find_listing_dicts_in_html_json_script_type():
    html = (
        '<html><body><script type="application/json">'
        f"{json.dumps(MINIMAL_LISTING)}"
        "</script></body></html>"
    )
    results = find_listing_dicts_in_html(html)
    assert len(results) == 1


def test_find_listing_dicts_in_html_nested_inside_larger_state():
    wrapper = {
        "user": {"email": "ben@example.com"},
        "consumerToursheet": {"waypoints": [{"listingObject": MINIMAL_LISTING}]},
    }
    html = f"<html><body><script>window.uc = {json.dumps(wrapper)};</script></body></html>"
    results = find_listing_dicts_in_html(html)
    assert len(results) == 1
    assert results[0]["price"]["formatted"] == "$500,000"


def test_find_listing_dicts_in_html_no_listing_returns_empty():
    html = '<html><body><script>window.uc = {"foo": "bar"};</script></body></html>'
    assert find_listing_dicts_in_html(html) == []

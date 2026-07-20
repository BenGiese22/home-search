import pytest

from src.json_extract import extract_balanced_json


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

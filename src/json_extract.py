def extract_balanced_json(text: str, open_index: int) -> str:
    """Given text and the index of an opening '{', return the substring
    from open_index through the matching closing '}', treating braces
    inside double-quoted string literals as inert."""
    depth = 0
    in_string = False
    escape = False
    for i in range(open_index, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_index : i + 1]
    raise ValueError("No matching closing brace found")


import json
import re

from bs4 import BeautifulSoup

_ASSIGNMENT_RE = re.compile(r"=\s*(\{)")


def find_json_blobs(html: str) -> list[dict]:
    """Find every <script> tag in html and return every top-level JSON
    object literal it contains, whether that's a JSON-typed script body
    or a `someVar = {...};` assignment."""
    soup = BeautifulSoup(html, "html.parser")
    blobs: list[dict] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue

        script_type = script.get("type", "")
        if "json" in script_type:
            try:
                blobs.append(json.loads(text))
                continue
            except (json.JSONDecodeError, TypeError):
                pass

        for match in _ASSIGNMENT_RE.finditer(text):
            open_index = match.start(1)
            try:
                candidate = extract_balanced_json(text, open_index)
                blobs.append(json.loads(candidate))
            except (ValueError, json.JSONDecodeError):
                continue
    return blobs


def _looks_like_listing(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    location = obj.get("location")
    price = obj.get("price")
    media = obj.get("media")
    return (
        isinstance(location, dict)
        and "prettyAddress" in location
        and isinstance(price, dict)
        and "formatted" in price
        and isinstance(media, list)
    )


def _find_listing_dicts(node: object, found: list[dict]) -> None:
    if isinstance(node, dict):
        if _looks_like_listing(node):
            found.append(node)
        for value in node.values():
            _find_listing_dicts(value, found)
    elif isinstance(node, list):
        for item in node:
            _find_listing_dicts(item, found)


def find_listing_dicts_in_html(html: str) -> list[dict]:
    """Return every dict embedded anywhere in html's <script> tags that
    looks like a Compass listing object."""
    found: list[dict] = []
    for blob in find_json_blobs(html):
        _find_listing_dicts(blob, found)
    return found

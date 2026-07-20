# Scrape + View (v0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python tool that logs into Compass, scrapes listing data + photos (from a shared collection URL and/or individual listing URLs), and outputs a CSV (for Google Sheets) plus a local HTML photo gallery — with no AI scoring yet.

**Architecture:** A pure-function core (JSON extraction, listing parsing, CSV/gallery rendering, a per-listing JSON store for resumability) that is fully unit-testable without a live browser, plus a thin Playwright layer (auth, page scraping) that is implemented directly and verified with a manual smoke test, wired together by a single entry-point script.

**Tech Stack:** Python 3.10+, Playwright (sync API), BeautifulSoup4, requests, python-dotenv, pytest.

## Global Constraints

- Python 3.10+.
- No AI/Claude scoring in this phase — out of scope (spec: "Non-goals (v0)").
- No commute/location analysis, scheduling, database, or dashboard in this phase.
- `.env` holds all config; `.env.example` stays current; no hardcoded secrets.
- `.env`, `data/` (photos, per-listing store, auth state) are gitignored — personal scraped data and session state are never committed.
- Photo downloads and page scrapes must be resumable/idempotent — reruns must not redo completed work (spec: "Resumability / state").
- No automated tests against the live Compass site; live-browser code gets a manual smoke test instead (spec: "Testing").

---

## File Structure

```
home-search/
├── .env.example
├── .gitignore
├── requirements.txt
├── conftest.py
├── scrape.py                    # entry point
├── src/
│   ├── __init__.py
│   ├── config.py                # .env -> Config
│   ├── models.py                # Listing dataclass
│   ├── json_extract.py          # find embedded listing JSON in raw HTML
│   ├── listing_parser.py        # listing JSON dict -> Listing
│   ├── photos.py                # download photos for a listing
│   ├── store.py                 # per-listing JSON store (resumability)
│   ├── csv_writer.py            # Listing[] -> data/listings.csv
│   ├── gallery.py                # Listing[] -> data/gallery.html
│   ├── auth.py                  # Playwright login / storage state
│   └── scraper.py               # Playwright: scrape_listing, scrape_collection
└── tests/
    ├── fixtures/
    │   └── canossa_dr_listing.json
    ├── test_config.py
    ├── test_models.py
    ├── test_json_extract.py
    ├── test_listing_parser.py
    ├── test_photos.py
    ├── test_store.py
    ├── test_csv_writer.py
    ├── test_gallery.py
    └── test_scraper.py
```

---

### Task 1: Project scaffolding + config loader

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `conftest.py`
- Create: `src/__init__.py`
- Create: `src/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass (`compass_email: str`, `compass_password: str`, `collection_url: str | None`, `listing_urls: list[str]`) and `load_config(env: Mapping[str, str]) -> Config`, raising `ValueError` on missing/invalid config. Every later task that needs credentials or target URLs consumes this.

- [x] **Step 1: Create the directory layout and non-code scaffolding**

```bash
mkdir -p src tests/fixtures data
touch src/__init__.py
```

`requirements.txt`:
```
playwright==1.47.0
python-dotenv==1.0.1
beautifulsoup4==4.12.3
requests==2.32.3
pytest==8.3.3
```

`.gitignore`:
```
.env
data/
__pycache__/
*.pyc
.pytest_cache/
```

`.env.example`:
```
# Compass login credentials
COMPASS_EMAIL=
COMPASS_PASSWORD=

# Shared collection URL from your realtor (optional if using LISTING_URLS)
COMPASS_COLLECTION_URL=

# Comma-separated individual listing URLs (optional if using COMPASS_COLLECTION_URL)
LISTING_URLS=
```

`conftest.py` (repo root — makes `from src.x import y` work when running `pytest` from the repo root regardless of pytest version/config):
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
```

- [x] **Step 2: Write the failing test for config loading**

`tests/test_config.py`:
```python
import pytest

from src.config import load_config


def test_load_config_with_listing_urls():
    env = {
        "COMPASS_EMAIL": "ben@example.com",
        "COMPASS_PASSWORD": "hunter2",
        "LISTING_URLS": "https://a.example/1, https://a.example/2",
    }
    config = load_config(env)
    assert config.compass_email == "ben@example.com"
    assert config.compass_password == "hunter2"
    assert config.collection_url is None
    assert config.listing_urls == ["https://a.example/1", "https://a.example/2"]


def test_load_config_with_collection_url():
    env = {
        "COMPASS_EMAIL": "ben@example.com",
        "COMPASS_PASSWORD": "hunter2",
        "COMPASS_COLLECTION_URL": "https://compass.com/collections/abc",
    }
    config = load_config(env)
    assert config.collection_url == "https://compass.com/collections/abc"
    assert config.listing_urls == []


def test_load_config_missing_credentials_raises():
    with pytest.raises(ValueError, match="COMPASS_EMAIL"):
        load_config({})


def test_load_config_missing_urls_raises():
    env = {"COMPASS_EMAIL": "a@b.com", "COMPASS_PASSWORD": "x"}
    with pytest.raises(ValueError, match="COMPASS_COLLECTION_URL"):
        load_config(env)
```

- [x] **Step 3: Run test to verify it fails**

Run: `pip install -r requirements.txt && pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.config'`

- [x] **Step 4: Implement config.py**

`src/config.py`:
```python
from dataclasses import dataclass
from typing import Mapping


@dataclass
class Config:
    compass_email: str
    compass_password: str
    collection_url: str | None
    listing_urls: list[str]


def load_config(env: Mapping[str, str]) -> Config:
    email = (env.get("COMPASS_EMAIL") or "").strip()
    password = (env.get("COMPASS_PASSWORD") or "").strip()
    if not email or not password:
        raise ValueError("COMPASS_EMAIL and COMPASS_PASSWORD must be set in .env")

    collection_url = (env.get("COMPASS_COLLECTION_URL") or "").strip() or None
    raw_listing_urls = (env.get("LISTING_URLS") or "").strip()
    listing_urls = (
        [u.strip() for u in raw_listing_urls.split(",") if u.strip()]
        if raw_listing_urls
        else []
    )

    if not collection_url and not listing_urls:
        raise ValueError(
            "Set at least one of COMPASS_COLLECTION_URL or LISTING_URLS in .env"
        )

    return Config(
        compass_email=email,
        compass_password=password,
        collection_url=collection_url,
        listing_urls=listing_urls,
    )
```

- [x] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (4 tests)

- [x] **Step 6: Commit**

```bash
git add requirements.txt .gitignore .env.example conftest.py src/__init__.py src/config.py tests/test_config.py
git commit -m "feat(config): add .env config loader"
```

---

### Task 2: Listing data model

**Files:**
- Create: `src/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Listing` dataclass with fields `listing_id: str`, `address: str`, `city: str`, `state: str`, `zip_code: str`, `price: str`, `beds: int`, `baths: float`, `sqft: int`, `lot_sqft: int`, `year_built: int`, `description: str`, `amenities: list[str]`, `photo_urls: list[str]`, `listing_url: str`. Used by every downstream task (parser, store, csv_writer, gallery, scraper).

- [x] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
from src.models import Listing


def test_listing_construction():
    listing = Listing(
        listing_id="2145067054346865465",
        address="2765 Canossa Drive",
        city="Broomfield",
        state="CO",
        zip_code="80020",
        price="$649,500",
        beds=4,
        baths=3.5,
        sqft=2268,
        lot_sqft=6726,
        year_built=1999,
        description="Beautifully renovated...",
        amenities=["Renovated Kitchen", "Private Yard"],
        photo_urls=["https://example.com/1.jpg"],
        listing_url="https://www.compass.com/homedetails/2765-Canossa-Dr/",
    )
    assert listing.address == "2765 Canossa Drive"
    assert listing.beds == 4
    assert listing.baths == 3.5
    assert listing.amenities == ["Renovated Kitchen", "Private Yard"]
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.models'`

- [x] **Step 3: Implement models.py**

`src/models.py`:
```python
from dataclasses import dataclass


@dataclass
class Listing:
    listing_id: str
    address: str
    city: str
    state: str
    zip_code: str
    price: str
    beds: int
    baths: float
    sqft: int
    lot_sqft: int
    year_built: int
    description: str
    amenities: list[str]
    photo_urls: list[str]
    listing_url: str
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS (1 test)

- [x] **Step 5: Commit**

```bash
git add src/models.py tests/test_models.py
git commit -m "feat(models): add Listing dataclass"
```

---

### Task 3: Balanced-brace JSON extraction

**Files:**
- Create: `src/json_extract.py`
- Test: `tests/test_json_extract.py`

**Interfaces:**
- Produces: `extract_balanced_json(text: str, open_index: int) -> str`, which returns the substring from `open_index` (the position of an opening `{`) through its matching closing `}`, treating characters inside string literals as inert. Raises `ValueError` if no matching brace is found. Used by Task 4 to pull JSON object literals out of `<script>` bodies.

- [x] **Step 1: Write the failing tests**

`tests/test_json_extract.py`:
```python
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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_json_extract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.json_extract'`

- [x] **Step 3: Implement extract_balanced_json**

`src/json_extract.py`:
```python
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
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_json_extract.py -v`
Expected: PASS (5 tests)

- [x] **Step 5: Commit**

```bash
git add src/json_extract.py tests/test_json_extract.py
git commit -m "feat(json_extract): add balanced-brace JSON extraction"
```

---

### Task 4: Locate listing-shaped JSON anywhere in a page

**Files:**
- Modify: `src/json_extract.py`
- Modify: `tests/test_json_extract.py`

**Interfaces:**
- Consumes: `extract_balanced_json` from Task 3.
- Produces: `find_listing_dicts_in_html(html: str) -> list[dict]` — parses every `<script>` tag in `html`, extracts every JSON object literal it can find (both `<script type="application/json">`/`ld+json` bodies and `= {...}` assignments like `window.uc = {...}`), recursively searches each parsed value for dicts that look like a Compass listing object (has `location.prettyAddress`, `price.formatted`, and a `media` list), and returns all matches. Used by Task 11's `scrape_listing`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_json_extract.py`:
```python
import json

from src.json_extract import find_listing_dicts_in_html

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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_json_extract.py -v`
Expected: FAIL with `ImportError: cannot import name 'find_listing_dicts_in_html'`

- [x] **Step 3: Implement the blob-finding and shape-matching logic**

Append to `src/json_extract.py`:
```python
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
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_json_extract.py -v`
Expected: PASS (9 tests)

- [x] **Step 5: Commit**

```bash
git add src/json_extract.py tests/test_json_extract.py
git commit -m "feat(json_extract): find listing-shaped JSON anywhere in a page"
```

---

### Task 5: Parse a listing JSON object into a Listing

**Files:**
- Create: `src/listing_parser.py`
- Create: `tests/fixtures/canossa_dr_listing.json`
- Test: `tests/test_listing_parser.py`

**Interfaces:**
- Consumes: `Listing` from Task 2.
- Produces: `parse_listing_object(obj: dict, listing_url: str) -> Listing`. Used by Task 11's `scrape_listing`.

- [x] **Step 1: Create the real-data fixture**

`tests/fixtures/canossa_dr_listing.json` (trimmed but real data — the home Ben confirmed as a "yes"):
```json
{
  "listingIdSHA": "2145067054346865465",
  "location": {
    "prettyAddress": "2765 Canossa Drive",
    "city": "Broomfield",
    "state": "CO",
    "zipCode": "80020"
  },
  "size": {
    "bedrooms": 4,
    "fullBathrooms": 3,
    "halfBathrooms": 1,
    "threeQuarterBathrooms": 0,
    "squareFeet": 2268,
    "lotSizeInSquareFeet": 6726
  },
  "price": {
    "formatted": "$649,500"
  },
  "buildingInfo": {
    "buildingYearOpened": 1999
  },
  "description": "Beautifully renovated and move-in-ready Broomfield charmer. With four bedrooms, four bathrooms, and a rare, peaceful, large private backyard, this lovely home is priced to sell.",
  "detailedInfo": {
    "amenities": [
      "Attached Garage",
      "Basement",
      "Renovated Kitchen",
      "Private Yard",
      "Walk-in Closet"
    ]
  },
  "media": [
    {"originalUrl": "https://www.compass.com/m/408ddf37e2ded171addb547c9487af6f5da7231549ce355089fdcf349cdaffb8/origin.jpg"},
    {"originalUrl": "https://www.compass.com/m/4898bd3b1d9da3ec804b32ef39dd88bed35e502f0d19370bc8102e5dff38c872/origin.jpg"}
  ]
}
```

- [x] **Step 2: Write the failing test**

`tests/test_listing_parser.py`:
```python
import json
from pathlib import Path

from src.listing_parser import parse_listing_object

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "canossa_dr_listing.json"
LISTING_URL = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/12NNXK_pid/"


def test_parse_listing_object_from_real_fixture():
    obj = json.loads(FIXTURE_PATH.read_text())
    listing = parse_listing_object(obj, listing_url=LISTING_URL)

    assert listing.listing_id == "2145067054346865465"
    assert listing.address == "2765 Canossa Drive"
    assert listing.city == "Broomfield"
    assert listing.state == "CO"
    assert listing.zip_code == "80020"
    assert listing.price == "$649,500"
    assert listing.beds == 4
    assert listing.baths == 3.5  # 3 full + 1 half
    assert listing.sqft == 2268
    assert listing.lot_sqft == 6726
    assert listing.year_built == 1999
    assert "renovated" in listing.description.lower()
    assert "Renovated Kitchen" in listing.amenities
    assert len(listing.photo_urls) == 2
    assert listing.photo_urls[0].startswith("https://www.compass.com/m/")
    assert listing.listing_url == LISTING_URL


def test_parse_listing_object_missing_optional_fields_defaults_safely():
    obj = {
        "location": {"prettyAddress": "1 Test St", "city": "X", "state": "CO", "zipCode": "00000"},
        "size": {"bedrooms": 2, "fullBathrooms": 1, "squareFeet": 900, "lotSizeInSquareFeet": 0},
        "price": {"formatted": "$1"},
        "media": [],
    }
    listing = parse_listing_object(obj, listing_url="https://example.com/1")
    assert listing.baths == 1.0
    assert listing.year_built == 0
    assert listing.amenities == []
    assert listing.photo_urls == []
```

- [x] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_listing_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.listing_parser'`

- [x] **Step 4: Implement parse_listing_object**

`src/listing_parser.py`:
```python
from src.models import Listing


def parse_listing_object(obj: dict, listing_url: str) -> Listing:
    location = obj.get("location", {})
    size = obj.get("size", {})
    price = obj.get("price", {})
    building = obj.get("buildingInfo", {})
    detailed = obj.get("detailedInfo", {})
    media = obj.get("media", [])

    full_baths = size.get("fullBathrooms", 0) or 0
    half_baths = size.get("halfBathrooms", 0) or 0
    three_quarter_baths = size.get("threeQuarterBathrooms", 0) or 0
    baths = full_baths + half_baths * 0.5 + three_quarter_baths * 0.75

    return Listing(
        listing_id=obj.get("listingIdSHA") or obj.get("feedListingId", ""),
        address=location.get("prettyAddress", ""),
        city=location.get("city", ""),
        state=location.get("state", ""),
        zip_code=location.get("zipCode", ""),
        price=price.get("formatted", ""),
        beds=size.get("bedrooms", 0) or 0,
        baths=baths,
        sqft=size.get("squareFeet", 0) or 0,
        lot_sqft=size.get("lotSizeInSquareFeet", 0) or 0,
        year_built=building.get("buildingYearOpened", 0) or 0,
        description=obj.get("description", ""),
        amenities=list(detailed.get("amenities", [])),
        photo_urls=[m["originalUrl"] for m in media if "originalUrl" in m],
        listing_url=listing_url,
    )
```

- [x] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_listing_parser.py -v`
Expected: PASS (2 tests)

- [x] **Step 6: Commit**

```bash
git add src/listing_parser.py tests/fixtures/canossa_dr_listing.json tests/test_listing_parser.py
git commit -m "feat(listing_parser): parse listing JSON into Listing"
```

---

### Task 6: Photo downloader

**Files:**
- Create: `src/photos.py`
- Test: `tests/test_photos.py`

**Interfaces:**
- Produces: `download_photos(photo_urls: list[str], dest_dir: Path, fetch_bytes: Callable[[str], bytes]) -> list[Path]`. `fetch_bytes` is injected so tests never hit the network. Skips re-downloading files that already exist (resumability). Used by Task 12's entry point.

- [x] **Step 1: Write the failing tests**

`tests/test_photos.py`:
```python
from pathlib import Path

from src.photos import download_photos


def test_download_photos_writes_numbered_files(tmp_path: Path):
    calls = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        return f"bytes for {url}".encode()

    dest_dir = tmp_path / "listing-1"
    urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]

    saved = download_photos(urls, dest_dir, fake_fetch)

    assert saved == [dest_dir / "01.jpg", dest_dir / "02.jpg"]
    assert (dest_dir / "01.jpg").read_bytes() == b"bytes for https://example.com/a.jpg"
    assert (dest_dir / "02.jpg").read_bytes() == b"bytes for https://example.com/b.jpg"
    assert calls == urls


def test_download_photos_skips_existing_files(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    dest_dir.mkdir(parents=True)
    (dest_dir / "01.jpg").write_bytes(b"already here")

    calls = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        return b"new bytes"

    download_photos(["https://example.com/a.jpg"], dest_dir, fake_fetch)

    assert calls == []
    assert (dest_dir / "01.jpg").read_bytes() == b"already here"


def test_download_photos_skips_photo_on_fetch_failure(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    urls = ["https://example.com/bad.jpg", "https://example.com/good.jpg"]

    def flaky_fetch(url: str) -> bytes:
        if "bad" in url:
            raise RuntimeError("network error")
        return b"good bytes"

    saved = download_photos(urls, dest_dir, flaky_fetch)

    assert saved == [dest_dir / "02.jpg"]
    assert not (dest_dir / "01.jpg").exists()
    assert (dest_dir / "02.jpg").read_bytes() == b"good bytes"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_photos.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.photos'`

- [x] **Step 3: Implement download_photos**

`src/photos.py`:
```python
from pathlib import Path
from typing import Callable


def download_photos(
    photo_urls: list[str],
    dest_dir: Path,
    fetch_bytes: Callable[[str], bytes],
) -> list[Path]:
    """Download each photo to dest_dir/NN.jpg, skipping files that already
    exist. A failure fetching one photo is logged and skipped rather than
    aborting the rest of the listing's photos."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, url in enumerate(photo_urls, start=1):
        dest = dest_dir / f"{i:02d}.jpg"
        if dest.exists():
            saved.append(dest)
            continue
        try:
            dest.write_bytes(fetch_bytes(url))
        except Exception as exc:
            print(f"skip photo (failed to download {url}): {exc}")
            continue
        saved.append(dest)
    return saved
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_photos.py -v`
Expected: PASS (3 tests)

- [x] **Step 5: Commit**

```bash
git add src/photos.py tests/test_photos.py
git commit -m "feat(photos): add resumable photo downloader"
```

---

### Task 7: Per-listing JSON store (resumability)

**Files:**
- Create: `src/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Listing` from Task 2.
- Produces: `is_scraped(store_dir: Path, listing_id: str) -> bool`, `save_listing(store_dir: Path, listing: Listing) -> None`, `load_all_listings(store_dir: Path) -> list[Listing]`. Used by Task 12's entry point to skip already-scraped listings and to regenerate CSV/gallery from everything scraped so far.

- [x] **Step 1: Write the failing tests**

`tests/test_store.py`:
```python
from pathlib import Path

from src.models import Listing
from src.store import is_scraped, load_all_listings, save_listing

SAMPLE = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$1",
    beds=2,
    baths=1.0,
    sqft=900,
    lot_sqft=1000,
    year_built=2000,
    description="desc",
    amenities=["A", "B"],
    photo_urls=["https://example.com/1.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_is_scraped_false_when_never_saved(tmp_path: Path):
    assert is_scraped(tmp_path, "abc123") is False


def test_save_and_is_scraped(tmp_path: Path):
    save_listing(tmp_path, SAMPLE)
    assert is_scraped(tmp_path, "abc123") is True


def test_load_all_listings_round_trips(tmp_path: Path):
    save_listing(tmp_path, SAMPLE)
    loaded = load_all_listings(tmp_path)
    assert loaded == [SAMPLE]


def test_load_all_listings_empty_dir_returns_empty_list(tmp_path: Path):
    assert load_all_listings(tmp_path / "does-not-exist") == []
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.store'`

- [x] **Step 3: Implement store.py**

`src/store.py`:
```python
import json
from dataclasses import asdict
from pathlib import Path

from src.models import Listing


def _listing_path(store_dir: Path, listing_id: str) -> Path:
    return store_dir / f"{listing_id}.json"


def is_scraped(store_dir: Path, listing_id: str) -> bool:
    return _listing_path(store_dir, listing_id).exists()


def save_listing(store_dir: Path, listing: Listing) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    _listing_path(store_dir, listing.listing_id).write_text(
        json.dumps(asdict(listing), indent=2)
    )


def load_all_listings(store_dir: Path) -> list[Listing]:
    if not store_dir.exists():
        return []
    listings = []
    for path in sorted(store_dir.glob("*.json")):
        data = json.loads(path.read_text())
        listings.append(Listing(**data))
    return listings
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: PASS (4 tests)

- [x] **Step 5: Commit**

```bash
git add src/store.py tests/test_store.py
git commit -m "feat(store): add per-listing JSON store for resumability"
```

---

### Task 8: CSV writer

**Files:**
- Create: `src/csv_writer.py`
- Test: `tests/test_csv_writer.py`

**Interfaces:**
- Consumes: `Listing` from Task 2.
- Produces: `write_csv(listings: list[Listing], photos_root: Path, path: Path) -> None`. Used by Task 12's entry point.

- [x] **Step 1: Write the failing test**

`tests/test_csv_writer.py`:
```python
import csv
from pathlib import Path

from src.csv_writer import write_csv
from src.models import Listing

LISTING = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$500,000",
    beds=3,
    baths=2.5,
    sqft=1800,
    lot_sqft=6000,
    year_built=1995,
    description="A lovely home",
    amenities=["Renovated Kitchen", "Private Yard"],
    photo_urls=["https://example.com/1.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_write_csv_round_trips_fields(tmp_path: Path):
    csv_path = tmp_path / "listings.csv"
    photos_root = tmp_path / "photos"

    write_csv([LISTING], photos_root, csv_path)

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    row = rows[0]
    assert row["listing_id"] == "abc123"
    assert row["address"] == "1 Test St"
    assert row["price"] == "$500,000"
    assert row["beds"] == "3"
    assert row["baths"] == "2.5"
    assert row["amenities"] == "Renovated Kitchen; Private Yard"
    assert row["listing_url"] == "https://example.com/listing/abc123"
    assert row["photo_dir"] == str(photos_root / "abc123")
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_csv_writer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.csv_writer'`

- [x] **Step 3: Implement write_csv**

`src/csv_writer.py`:
```python
import csv
from pathlib import Path

from src.models import Listing

FIELDNAMES = [
    "listing_id",
    "address",
    "city",
    "state",
    "zip_code",
    "price",
    "beds",
    "baths",
    "sqft",
    "lot_sqft",
    "year_built",
    "description",
    "amenities",
    "listing_url",
    "photo_dir",
]


def write_csv(listings: list[Listing], photos_root: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for listing in listings:
            writer.writerow(
                {
                    "listing_id": listing.listing_id,
                    "address": listing.address,
                    "city": listing.city,
                    "state": listing.state,
                    "zip_code": listing.zip_code,
                    "price": listing.price,
                    "beds": listing.beds,
                    "baths": listing.baths,
                    "sqft": listing.sqft,
                    "lot_sqft": listing.lot_sqft,
                    "year_built": listing.year_built,
                    "description": listing.description,
                    "amenities": "; ".join(listing.amenities),
                    "listing_url": listing.listing_url,
                    "photo_dir": str(photos_root / listing.listing_id),
                }
            )
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_csv_writer.py -v`
Expected: PASS (1 test)

- [x] **Step 5: Commit**

```bash
git add src/csv_writer.py tests/test_csv_writer.py
git commit -m "feat(csv_writer): write listings.csv"
```

---

### Task 9: HTML gallery writer

**Files:**
- Create: `src/gallery.py`
- Test: `tests/test_gallery.py`

**Interfaces:**
- Consumes: `Listing` from Task 2.
- Produces: `render_gallery(listings: list[Listing], photos_root: Path, gallery_dir: Path) -> str` and `write_gallery(listings: list[Listing], photos_root: Path, path: Path) -> None`. Used by Task 12's entry point.

- [x] **Step 1: Write the failing tests**

`tests/test_gallery.py`:
```python
from pathlib import Path

from src.gallery import render_gallery, write_gallery
from src.models import Listing

LISTING = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$500,000",
    beds=3,
    baths=2.5,
    sqft=1800,
    lot_sqft=6000,
    year_built=1995,
    description="A lovely home",
    amenities=["Renovated Kitchen", "Private Yard"],
    photo_urls=["https://example.com/1.jpg", "https://example.com/2.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_render_gallery_includes_listing_details_and_photos(tmp_path: Path):
    photos_root = tmp_path / "photos"
    photo_dir = photos_root / "abc123"
    photo_dir.mkdir(parents=True)
    (photo_dir / "01.jpg").write_bytes(b"fake")
    (photo_dir / "02.jpg").write_bytes(b"fake")
    gallery_dir = tmp_path  # gallery.html would live at tmp_path/gallery.html

    html = render_gallery([LISTING], photos_root, gallery_dir)

    assert "1 Test St" in html
    assert "$500,000" in html
    assert "Renovated Kitchen" in html
    assert "photos/abc123/01.jpg" in html
    assert "photos/abc123/02.jpg" in html
    assert "https://example.com/listing/abc123" in html


def test_render_gallery_handles_listing_with_no_downloaded_photos(tmp_path: Path):
    photos_root = tmp_path / "photos"
    html = render_gallery([LISTING], photos_root, tmp_path)
    assert "1 Test St" in html


def test_write_gallery_writes_file(tmp_path: Path):
    photos_root = tmp_path / "photos"
    gallery_path = tmp_path / "gallery.html"

    write_gallery([LISTING], photos_root, gallery_path)

    assert gallery_path.exists()
    assert "1 Test St" in gallery_path.read_text()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gallery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.gallery'`

- [x] **Step 3: Implement gallery.py**

`src/gallery.py`:
```python
import os
from pathlib import Path

from src.models import Listing


def render_gallery(listings: list[Listing], photos_root: Path, gallery_dir: Path) -> str:
    sections = []
    for listing in listings:
        photo_dir = photos_root / listing.listing_id
        photo_files = sorted(photo_dir.glob("*.jpg")) if photo_dir.exists() else []
        rel_srcs = [os.path.relpath(p, start=gallery_dir) for p in photo_files]
        photos_html = "".join(f'<img src="{src}" loading="lazy">' for src in rel_srcs)

        sections.append(
            f"""
        <section class="listing">
          <h2>{listing.address}, {listing.city}, {listing.state} {listing.zip_code}</h2>
          <p>{listing.price} &middot; {listing.beds} bd &middot; {listing.baths} ba &middot; {listing.sqft} sqft</p>
          <p>{listing.description}</p>
          <p>Amenities: {", ".join(listing.amenities)}</p>
          <p><a href="{listing.listing_url}">View on Compass</a></p>
          <div class="photos">{photos_html}</div>
        </section>"""
        )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Home Search Gallery</title>
<style>
  body {{ font-family: sans-serif; max-width: 900px; margin: 0 auto; padding: 16px; }}
  .listing {{ border-bottom: 1px solid #ccc; padding: 16px 0; }}
  .photos img {{ width: 200px; margin: 4px; border-radius: 4px; }}
</style>
</head>
<body>{"".join(sections)}</body>
</html>"""


def write_gallery(listings: list[Listing], photos_root: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_gallery(listings, photos_root, path.parent), encoding="utf-8")
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gallery.py -v`
Expected: PASS (3 tests)

- [x] **Step 5: Commit**

```bash
git add src/gallery.py tests/test_gallery.py
git commit -m "feat(gallery): write local HTML photo gallery"
```

---

### Task 10: Playwright auth (login + storage state)

**Files:**
- Create: `src/auth.py`

**Interfaces:**
- Produces: `ensure_logged_in(context: BrowserContext, page: Page, url: str, email: str, password: str, storage_state_path: Path) -> None`, raising `RuntimeError` with a clear message if the password field is still present after submit (spec: "Login failure → fail fast with a clear message"). Used by Task 12's entry point. Not unit tested — requires a live browser and real Compass credentials (spec: "No automated tests against the live Compass site"); the flow below was verified live against the real Compass login form during plan execution (see Task 12's manual smoke test for the remaining end-to-end check).

- [x] **Step 1: Implement ensure_logged_in**

Compass's real login form (verified live, not a guess) is a **two-step email-then-password flow**: `url` shows only an email field and a "Continue" button; submitting it swaps in a password field (with a short client-side delay, so it must be waited for explicitly) and a "Sign In" button that has no `type="submit"` attribute — it must be matched by its text.

`src/auth.py`:
```python
from pathlib import Path

from playwright.sync_api import BrowserContext, Page


def ensure_logged_in(
    context: BrowserContext,
    page: Page,
    url: str,
    email: str,
    password: str,
    storage_state_path: Path,
) -> None:
    """Navigate to url. If a login form appears (no valid session in the
    context's storage state), complete Compass's two-step email-then-password
    login and submit. Always persist the resulting session to
    storage_state_path so future runs can skip login.
    """
    page.goto(url)
    email_input = page.locator('input[type="email"]')
    if email_input.count() > 0:
        email_input.first.fill(email)
        page.locator('button[type="submit"]').first.click()
        page.wait_for_selector('input[type="password"]', timeout=10000)

        page.locator('input[type="password"]').first.fill(password)
        page.locator('button:has-text("Sign In")').first.click()
        page.wait_for_load_state("networkidle")

        if page.locator('input[type="password"]').count() > 0:
            raise RuntimeError(
                "Compass login failed: the password field is still showing "
                "after Sign In. Check COMPASS_EMAIL/COMPASS_PASSWORD in .env, "
                "and verify the selectors in ensure_logged_in still match "
                "Compass's real login form."
            )

    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_state_path))
```

- [x] **Step 2: Commit**

```bash
git add src/auth.py
git commit -m "feat(auth): add Playwright login and session persistence"
```

---

### Task 11: Playwright scraping (single listing + collection)

**Files:**
- Create: `src/scraper.py`
- Test: `tests/test_scraper.py`

**Interfaces:**
- Consumes: `find_listing_dicts_in_html` (Task 4), `parse_listing_object` (Task 5), `Listing` (Task 2).
- Produces: `derive_listing_id_from_url(url: str) -> str | None` (unit tested), `scrape_listing(page: Page, url: str) -> Listing` and `scrape_collection(page: Page, collection_url: str) -> list[str]` (not unit tested — require a live browser; verified in Task 12's manual smoke test). Used by Task 12's entry point.

- [x] **Step 1: Write the failing tests for derive_listing_id_from_url**

`tests/test_scraper.py`:
```python
from src.scraper import derive_listing_id_from_url


def test_derive_listing_id_from_url_homedetails_lid():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/2145067054346865465_lid/"
    assert derive_listing_id_from_url(url) == "2145067054346865465"


def test_derive_listing_id_from_url_listing_view():
    url = "https://www.compass.com/listing/2130651237632606465/view?agent_id=688995414728a40001928728"
    assert derive_listing_id_from_url(url) == "2130651237632606465"


def test_derive_listing_id_from_url_no_id_returns_none():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/"
    assert derive_listing_id_from_url(url) is None
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scraper.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.scraper'`

- [x] **Step 3: Implement scraper.py**

`src/scraper.py`:
```python
import re

from playwright.sync_api import Page

from src.json_extract import find_listing_dicts_in_html
from src.listing_parser import parse_listing_object
from src.models import Listing

_LISTING_ID_RE = re.compile(r"(\d{10,})")


def derive_listing_id_from_url(url: str) -> str | None:
    """Best-effort extraction of a Compass listing ID from its URL, used
    only as a cheap pre-fetch resumability check. The authoritative ID
    always comes from the scraped page's own listingIdSHA (see
    parse_listing_object); if this heuristic ever mismatches, the worst
    case is one extra page load, not a data-correctness bug."""
    match = _LISTING_ID_RE.search(url)
    return match.group(1) if match else None


def scrape_listing(page: Page, url: str) -> Listing:
    page.goto(url)
    page.wait_for_load_state("networkidle")
    html = page.content()
    candidates = find_listing_dicts_in_html(html)
    if not candidates:
        raise ValueError(f"No listing data found on page: {url}")
    return parse_listing_object(candidates[0], listing_url=url)


def scrape_collection(page: Page, collection_url: str) -> list[str]:
    """Scroll a Compass collection page until no new listing links load,
    then return every unique listing detail-page URL found."""
    page.goto(collection_url)
    page.wait_for_load_state("networkidle")

    previous_count = 0
    while True:
        links = page.eval_on_selector_all(
            'a[href*="/homedetails/"]',
            "elements => elements.map(e => e.href)",
        )
        unique_links = sorted(set(links))
        if len(unique_links) == previous_count:
            break
        previous_count = len(unique_links)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1000)

    return unique_links
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scraper.py -v`
Expected: PASS (3 tests)

- [x] **Step 5: Commit**

```bash
git add src/scraper.py tests/test_scraper.py
git commit -m "feat(scraper): add Playwright listing and collection scraping"
```

---

### Task 12: Entry point + manual smoke test

**Files:**
- Create: `scrape.py`

**Interfaces:**
- Consumes: everything from Tasks 1–11.
- Produces: the `scrape.py` CLI entry point; running it is the deliverable for this task.

- [x] **Step 1: Run the full test suite before wiring the entry point**

Run: `pytest -v`
Expected: PASS (all tests from Tasks 1–11, 30 tests total)

- [x] **Step 2: Implement scrape.py**

`scrape.py`:
```python
from pathlib import Path

import requests
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

from src.auth import ensure_logged_in
from src.config import load_config
from src.csv_writer import write_csv
from src.gallery import write_gallery
from src.photos import download_photos
from src.scraper import derive_listing_id_from_url, scrape_collection, scrape_listing
from src.store import is_scraped, load_all_listings, save_listing

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
STORE_DIR = DATA_DIR / "listings"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
CSV_PATH = DATA_DIR / "listings.csv"
GALLERY_PATH = DATA_DIR / "gallery.html"
LOGIN_URL = "https://www.compass.com/login/"


def fetch_bytes(url: str) -> bytes:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def main() -> None:
    config = load_config(dotenv_values(".env"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        storage_state = str(AUTH_STATE_PATH) if AUTH_STATE_PATH.exists() else None
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()

        ensure_logged_in(
            context, page, LOGIN_URL,
            config.compass_email, config.compass_password, AUTH_STATE_PATH,
        )

        listing_urls = list(config.listing_urls)
        if config.collection_url:
            listing_urls.extend(scrape_collection(page, config.collection_url))

        for url in listing_urls:
            precheck_id = derive_listing_id_from_url(url)
            if precheck_id and is_scraped(STORE_DIR, precheck_id):
                print(f"skip (already scraped): {url}")
                continue

            try:
                listing = scrape_listing(page, url)
                if is_scraped(STORE_DIR, listing.listing_id):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                download_photos(listing.photo_urls, PHOTOS_DIR / listing.listing_id, fetch_bytes)
                save_listing(STORE_DIR, listing)
                print(f"scraped: {listing.address}")
            except Exception as exc:
                print(f"skip listing (failed to scrape {url}): {exc}")
                continue

        browser.close()

    all_listings = load_all_listings(STORE_DIR)
    write_csv(all_listings, PHOTOS_DIR, CSV_PATH)
    write_gallery(all_listings, PHOTOS_DIR, GALLERY_PATH)
    print(f"\nWrote {len(all_listings)} listings to {CSV_PATH} and {GALLERY_PATH}")


if __name__ == "__main__":
    main()
```

- [x] **Step 3: Commit**

```bash
git add scrape.py
git commit -m "feat(scrape): wire up entry point"
```

- [x] **Step 4: Install the Playwright browser binary**

Run: `playwright install chromium`
Expected: Chromium downloads successfully.

- [x] **Step 5: Verify credentials are in place**

`.env` was already created and populated with real `COMPASS_EMAIL`/`COMPASS_PASSWORD`/`LISTING_URLS` during plan execution (before this task was dispatched), while verifying the login flow live. **Do not run `cp .env.example .env`** — that would overwrite it and destroy the real credentials. Just confirm the file exists and has all three values set:

```bash
test -f .env && grep -c '=.\+' .env
```

Expected: `.env` exists and the count is at least 3 (email, password, listing URLs all non-empty). If it's missing or incomplete, stop and report NEEDS_CONTEXT rather than creating a fresh one from the template.

- [x] **Step 6: Manual smoke test — run against the two known listings**

Run: `python scrape.py`

Expected and actually confirmed live during plan execution: a headless Chromium instance logs into Compass (`headless=True` because this environment has no display, and it's the more portable default for a script that may run unattended). Individual listing pages don't require login at all when accessed anonymously, but `scrape.py` always authenticates first via `ensure_logged_in` regardless of mode (it's a no-op if a session is already valid), and with that authenticated session both known listings scrape cleanly:

- `.../listing/2130651237632606465/view` (4552 W 111th Ave, Westminster) scrapes successfully.
- `.../listing/2120506603298373729/view` (8221 93rd Way, Broomfield) *also* scrapes successfully once authenticated — an earlier, unauthenticated discovery check had seen this listing with redacted price/sqft and expected it to fail the shape check, but the real pipeline's authenticated session sees the full listing data. (Historical note only — do not "fix" `scrape_listing`/`find_listing_dicts_in_html` to reproduce the old unauthenticated-failure expectation; both listings scraping successfully is the correct, verified outcome.)
- The run finishes with `Wrote 2 listings to data/listings.csv and data/gallery.html`.

If either URL fails, or login fails: inspect `page.content()` at the failure point — Compass's markup may have changed since verification.

**Known limitation, confirmed live and out of scope for this fix pass:** `scrape_collection`'s current approach (scan `page.content()` for `a[href*="/homedetails/"]` links) does not work against Compass's real collection UI — it's a client-rendered SPA at `/app/collection/<id>/matches...` that returns zero matching links even when authenticated, not a simple scrollable static page. Only `LISTING_URLS` mode is currently verified working end-to-end; treat `COMPASS_COLLECTION_URL` as non-functional until a follow-up investigates the SPA's real data-loading mechanism (likely network-request interception to find Compass's internal API, not a CSS-selector tweak).

- [x] **Step 7: Verify outputs**

Open `data/listings.csv` — confirm two rows (4552 W 111th Ave, 8221 93rd Way) with correct addresses/prices, and that the `listing_url` column is a working Compass link for each.
Open `data/gallery.html` in a browser — confirm two sections with photos rendering and working "View on Compass" links.

- [x] **Step 8: Verify resumability**

Run: `python scrape.py` again.
Expected and confirmed live: both listings print `skip (already scraped): ...` and no new photos are downloaded for either (verified via unchanged `data/photos/<id>/` file mtimes) — resumability works correctly.

- [x] **Step 9: Commit any fixes made during the smoke test**

```bash
git add -A
git commit -m "fix: adjust scraping selectors based on live Compass smoke test"
```

(Skip this step if no fixes were needed.)

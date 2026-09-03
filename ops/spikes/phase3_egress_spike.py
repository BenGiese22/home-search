"""Phase 3 gate spikes: Nominatim/OSRM and the Compass photo CDN, from
whatever IP this runs on.

Self-contained on purpose -- it has to run in a bare sandbox with only
`requests`. Uses the project's real endpoints and User-Agent so the result
says something about the actual pipeline, not a proxy for it.

Run it locally and in a Vercel Sandbox; the only variable is the egress IP.
"""
import json
import sys
import time
from urllib.parse import quote

import requests

# Exactly what compute_commutes.py sends.
USER_AGENT = "home-search/1.0 (bengiese22@gmail.com)"

ADDRESSES = [
    "8221 West 93rd Way, Westminster, CO 80021",
    "5012 West 77th Drive, Westminster, CO 80030",
    "9313 West 91st Place, Broomfield, CO 80021",
]
PHOTO_URLS = [
    "https://www.compass.com/m/25a308ecd949c45520947d591f6c2c94fd041d75_img_0_9087c/origin.jpg",
    "https://www.compass.com/m/25a308ecd949c45520947d591f6c2c94fd041d75_img_1_bc7e2/origin.jpg",
]

result = {"where": sys.argv[1] if len(sys.argv) > 1 else "unknown"}


def egress_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=15).text.strip()
    except Exception as exc:
        return f"error: {exc}"


result["egress_ip"] = egress_ip()

# --- Gate 2: Nominatim ----------------------------------------------------
geo = []
for addr in ADDRESSES:
    time.sleep(1.0)  # Nominatim's documented rate limit
    entry = {"address": addr}
    try:
        r = requests.get(
            f"https://nominatim.openstreetmap.org/search?q={quote(addr)}&format=json&limit=1",
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        entry["status"] = r.status_code
        if r.ok:
            data = r.json()
            entry["hits"] = len(data)
            if data:
                entry["lat"] = data[0]["lat"]
                entry["lon"] = data[0]["lon"]
        else:
            entry["body"] = r.text[:200]
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    geo.append(entry)
result["nominatim"] = geo

# --- Gate 2b: OSRM routing ------------------------------------------------
osrm = {}
try:
    # Westminster CO -> Denver Union Station, the real shape of a commute leg.
    r = requests.get(
        "https://router.project-osrm.org/route/v1/driving/"
        "-105.0644,39.8617;-104.9998,39.7531?overview=false",
        timeout=30,
    )
    osrm["status"] = r.status_code
    if r.ok:
        d = r.json()
        osrm["code"] = d.get("code")
        if d.get("routes"):
            osrm["miles"] = round(d["routes"][0]["distance"] / 1609.34, 1)
            osrm["minutes"] = round(d["routes"][0]["duration"] / 60, 1)
    else:
        osrm["body"] = r.text[:200]
except Exception as exc:
    osrm["error"] = f"{type(exc).__name__}: {exc}"
result["osrm"] = osrm

# --- Gate 3: Compass photo CDN -------------------------------------------
photos = []
for url in PHOTO_URLS:
    entry = {"url": url.rsplit("/", 2)[-2][:24] + "..."}
    try:
        r = requests.get(url, timeout=30)
        entry["status"] = r.status_code
        entry["bytes"] = len(r.content)
        entry["content_type"] = r.headers.get("content-type")
        entry["is_jpeg"] = r.content[:2] == b"\xff\xd8"
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    photos.append(entry)
result["photo_cdn"] = photos

print(json.dumps(result, indent=2))

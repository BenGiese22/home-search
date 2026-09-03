"""Sustained Nominatim check: does a cloud IP get throttled over a run's
worth of requests? Production volume is only new listings, so 12 at the
documented 1 req/sec is already more than a typical run asks for."""
import json, sys, time
from urllib.parse import quote
import requests

UA = "home-search/1.0 (bengiese22@gmail.com)"
ADDRS = [
    "8221 West 93rd Way, Westminster, CO 80021",
    "5012 West 77th Drive, Westminster, CO 80030",
    "6799 West 52nd Avenue, Arvada, CO 80002",
    "9339 West 76th Avenue, Arvada, CO 80005",
    "3788 West 81st Avenue, Westminster, CO 80031",
    "4131 Snowbird Avenue, Broomfield, CO 80020",
    "7047 West 62nd Place, Arvada, CO 80003",
    "2911 North Princess Circle, Broomfield, CO 80020",
    "Denver Union Station, Denver, CO",
    "Medtronic, Lafayette, CO",
    "Lafayette, CO",
    "6222 West 70th Avenue, Arvada, CO 80003",
]
codes, hits, errors, t0 = [], 0, [], time.time()
for a in ADDRS:
    time.sleep(1.0)
    try:
        r = requests.get(
            f"https://nominatim.openstreetmap.org/search?q={quote(a)}&format=json&limit=1",
            headers={"User-Agent": UA}, timeout=30)
        codes.append(r.status_code)
        if r.ok and r.json():
            hits += 1
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        codes.append(None)
print(json.dumps({
    "where": sys.argv[1],
    "requests": len(ADDRS),
    "status_codes": codes,
    "all_200": all(c == 200 for c in codes),
    "throttled_429_or_403": sum(1 for c in codes if c in (429, 403)),
    "geocoded": hits,
    "elapsed_s": round(time.time() - t0, 1),
    "errors": errors,
}, indent=2))

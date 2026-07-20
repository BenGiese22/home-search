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

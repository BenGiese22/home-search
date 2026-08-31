import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

DEFAULT_ENV_PATH = ".env"


def load_env(dotenv_path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Configuration from `.env`, overridden by the process environment.

    Reading only `.env` means a script cannot run anywhere that supplies
    configuration as process environment variables -- which is every
    containerised or hosted environment, and a hard prerequisite for running
    any stage outside this laptop. Merging is harmless locally: with no
    process env set, every value is exactly what `.env` says.

    A missing `.env` is not an error; dotenv_values returns an empty mapping
    and the process environment supplies everything.

    Keys declared without a value (a bare `KEY` line) parse to None and are
    dropped rather than merged -- injecting None would shadow a real
    process-env value and defeat callers' defaults, both worse than treating
    the line as absent.
    """
    from_file = {
        key: value
        for key, value in dotenv_values(dotenv_path).items()
        if value is not None
    }
    return {**from_file, **os.environ}


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

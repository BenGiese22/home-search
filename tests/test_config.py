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

import pytest

from src.config import collection_tab_from_url, load_config


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


# --- collection tabs -------------------------------------------------------
#
# Compass serves matches and favorites from one collection ID, switched by an
# API filter rather than the URL path. These cover the config surface that
# decides which tabs get fetched, and the validation that stops a plausible
# but wrong URL from silently meaning something else.

MATCHES_URL = (
    "https://www.compass.com/app/collection/6a27426b698343000129b139/matches"
    "?source=deals&page=1&pageSize=120&sort=time_added"
)
FAVORITES_URL = "https://www.compass.com/app/collection/6a27426b698343000129b139/favorites"
NOT_INTERESTED_URL = (
    "https://www.compass.com/app/collection/6a27426b698343000129b139/notInterested"
)


def _env(**overrides):
    env = {
        "COMPASS_EMAIL": "ben@example.com",
        "COMPASS_PASSWORD": "hunter2",
        "COMPASS_COLLECTION_URL": MATCHES_URL,
    }
    env.update(overrides)
    return env


@pytest.mark.parametrize(
    "url, expected",
    [
        (MATCHES_URL, "matches"),
        (FAVORITES_URL, "favorites"),
        (NOT_INTERESTED_URL, "notInterested"),
        ("https://compass.com/collections/abc", None),
        ("https://www.compass.com/app/collection/6a27426b698343000129b139", None),
    ],
)
def test_collection_tab_from_url(url, expected):
    assert collection_tab_from_url(url) == expected


def test_untouched_matches_url_now_fetches_favorites_too():
    """The backward-compatibility guarantee: an existing .env keeps working
    and starts picking up favorites without being edited."""
    assert load_config(_env()).collection_tabs == ("favorites", "matches")


def test_favorites_url_is_accepted():
    assert load_config(_env(COMPASS_COLLECTION_URL=FAVORITES_URL)).collection_tabs == (
        "favorites",
        "matches",
    )


def test_not_interested_url_raises():
    with pytest.raises(ValueError, match="notInterested"):
        load_config(_env(COMPASS_COLLECTION_URL=NOT_INTERESTED_URL))


def test_unknown_url_tab_raises():
    with pytest.raises(ValueError, match="somethingElse"):
        load_config(
            _env(
                COMPASS_COLLECTION_URL=(
                    "https://www.compass.com/app/collection/abc123/somethingElse"
                )
            )
        )


def test_explicit_tabs_override_the_default():
    config = load_config(
        _env(COMPASS_COLLECTION_URL=FAVORITES_URL, COMPASS_COLLECTION_TABS="favorites")
    )
    assert config.collection_tabs == ("favorites",)


def test_explicit_tabs_tolerate_whitespace_and_apply_precedence():
    """Favorites is ordered first however it was typed -- dedup keeps the
    first copy of a listing, and the favorites copy is the one that wins."""
    config = load_config(_env(COMPASS_COLLECTION_TABS=" matches , favorites "))
    assert config.collection_tabs == ("favorites", "matches")


def test_duplicate_tabs_collapse():
    config = load_config(_env(COMPASS_COLLECTION_TABS="matches,matches"))
    assert config.collection_tabs == ("matches",)


def test_blank_tabs_fall_back_to_the_default():
    assert load_config(_env(COMPASS_COLLECTION_TABS="   ")).collection_tabs == (
        "favorites",
        "matches",
    )


def test_not_interested_in_explicit_tabs_raises():
    with pytest.raises(ValueError, match="notInterested"):
        load_config(_env(COMPASS_COLLECTION_TABS="matches,notInterested"))


def test_unknown_explicit_tab_raises():
    with pytest.raises(ValueError, match="bogus"):
        load_config(_env(COMPASS_COLLECTION_TABS="bogus"))


def test_url_tab_excluded_by_explicit_tabs_raises():
    with pytest.raises(ValueError, match="excludes it"):
        load_config(_env(COMPASS_COLLECTION_URL=FAVORITES_URL, COMPASS_COLLECTION_TABS="matches"))

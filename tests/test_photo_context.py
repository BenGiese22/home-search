import json, sqlite3
from score_photos import build_listing_context

def _row(**kw):
    base = dict(address="1 A St", city="Denver", state="CO", zip_code="80202",
                price="$500,000", beds=3, baths=2.0, sqft=2668, lot_sqft=6000,
                parking_spaces=2, year_built=1990, description="")
    base.update(kw)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    cols = ", ".join(base)
    c.execute(f"CREATE TABLE t ({cols})")
    c.execute(f"INSERT INTO t VALUES ({', '.join('?' * len(base))})", tuple(base.values()))
    return c.execute("SELECT * FROM t").fetchone()

def test_basement_present_is_stated():
    ctx = build_listing_context(_row(sqft_above_grade=1862, sqft_below_grade=725,
                                     outdoor_spaces=json.dumps(["Deck", "Patio"])), [])
    assert "1,862 sqft above grade, 725 sqft finished below grade" in ctx
    assert "a basement exists" in ctx
    assert "Outdoor features per the listing data: Deck, Patio" in ctx

def test_no_basement_is_stated_explicitly():
    ctx = build_listing_context(_row(sqft_above_grade=1867, sqft_below_grade=None,
                                     outdoor_spaces=None), [])
    assert "NO BASEMENT" in ctx
    assert "Outdoor features" not in ctx

def test_tolerates_pre_migration_row_without_new_columns():
    ctx = build_listing_context(_row(), [])
    assert "Address: 1 A St" in ctx
    assert "basement" not in ctx.lower()

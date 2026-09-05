"""Deleting a listing must go through the layer that reclaims its blobs.

`delete_listing` and `bulk_delete_listings` remove the `hosted_photos` rows,
and `hosted_photos.blob_url` is the ONLY record that an image was uploaded.
So a caller that deletes rows without first reading those URLs has not
merely forgotten to tidy up -- it has destroyed the only handle on the
blobs, permanently. That is how 1,813 orphans (~371 MB) accumulated.

The reclamation lives in src/diff.py's apply_delisting, which reads the URLs
first and deletes them after. Nothing else should be deleting listings, and
this test is what keeps that true -- the hazard is invisible at the call
site, and the cost of getting it wrong is silent and unrecoverable.

Parsed rather than grepped: a comment mentioning delete_listing (there are
several, explaining this exact history) is not a call, and an earlier guard
of this shape in check.py passed while the code was wrong because it matched
its own explanatory comment.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESTRUCTIVE = {"delete_listing", "bulk_delete_listings"}

# src/diff.py owns the reclamation and is the only place allowed to call
# them. src/db.py defines them.
ALLOWED = {"src/diff.py", "src/db.py"}

MODULES = sorted(
    str(p.relative_to(ROOT))
    for p in list(ROOT.glob("*.py")) + list(ROOT.glob("src/**/*.py")) + list(ROOT.glob("ops/**/*.py"))
    if "spikes" not in p.parts
)


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_there_are_modules_to_check():
    """A glob matching nothing would make every case below vacuous."""
    assert len(MODULES) > 10
    assert "src/diff.py" in MODULES


@pytest.mark.parametrize("module", MODULES)
def test_only_the_reclaiming_layer_deletes_listings(module):
    if module in ALLOWED:
        return
    called = _called_names(ROOT / module)
    offenders = sorted(called & DESTRUCTIVE)
    assert not offenders, (
        f"{module} calls {offenders} directly. Those remove the hosted_photos "
        f"rows, which are the only record of the uploaded blobs -- deleting "
        f"them without reading the URLs first strands the images permanently. "
        f"Go through src/diff.py's apply_delisting, which reclaims them."
    )


def test_the_reclaiming_layer_does_call_them():
    """The guard is only meaningful if the allowed module actually uses
    them -- otherwise this passes because nothing anywhere deletes."""
    assert _called_names(ROOT / "src/diff.py") & DESTRUCTIVE

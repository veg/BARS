"""The tool ships a copy of REVIEW_TEMPLATE.md; it must match the repo-root rubric byte for byte."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_packaged_template_matches_repo_root():
    packaged = REPO / "galaxy" / "bars_review" / "bars" / "REVIEW_TEMPLATE.md"
    root = REPO / "REVIEW_TEMPLATE.md"
    assert packaged.read_bytes() == root.read_bytes(), (
        "galaxy/bars_review/bars/REVIEW_TEMPLATE.md is out of sync; copy REVIEW_TEMPLATE.md over it"
    )


def test_review_module_hashes_packaged_template():
    import hashlib

    from bars import review

    packaged = REPO / "galaxy" / "bars_review" / "bars" / "REVIEW_TEMPLATE.md"
    assert review.TEMPLATE_PATH == packaged
    assert review.template_sha256() == hashlib.sha256(packaged.read_bytes()).hexdigest()

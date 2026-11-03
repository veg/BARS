"""Score extraction and validation (bars.parse)."""

import pytest

from bars.parse import parse_scores, tier_for

HEADER_BOLD = """\
# Review of MANUSCRIPT_001

**Manuscript Title:** [REDACTED FOR BLINDED REVIEW]
**Overall Score:** **7.2 / 10.0** (Strong / Sound) | **Scientific Core Score:** **6.0 / 8.0**
* **Score Breakdown:** Rigor: 2.6/3.5 | Claims: 1.8/2.5 | Biology: 1.6/2.0 | Reproducibility: 1.2/2.0

---
"""

# Variants copied from this repo's reviews/ (B22.md, the 2022 pilot entry, the 2026 meta-audit).
HEADER_LONG_LABELS = """\
**Overall Score:** 7.0 / 10.0 (Strong / Sound) | **Scientific Core Score:** 5.6 / 8.0
* **Score Breakdown:** Methodological Rigor: 2.2/3.5 | Claim-Evidence Alignment: 1.7/2.5 | Biological Plausibility: 1.7/2.0 | Reproducibility: 1.4/2.0
* **Rigor:** 2.2/3.5 | **Claims:** 1.7/2.5 | **Biology:** 1.7/2.0 | **Reproducibility:** 1.4/2.0
"""

HEADER_PILOT = """\
**Overall Score:** **2.5 / 10.0** (Fundamentally Invalid — Blinded Calibration Score; Initial Unblinded Pilot Score: **4.1 / 10.0**, Flawed / Overstated)
* **Blinded Score Breakdown (`ANON_37`):** Rigor: 0.5/3.5 | Claims: 0.4/2.5 | Biology: 0.5/2.0 | Reproducibility: 1.1/2.0
* **Initial Unblinded Pilot Score Breakdown:** Rigor: 1.2/3.5 | Claims: 0.8/2.5 | Biology: 0.7/2.0 | Reproducibility: 1.4/2.0
"""

HEADER_BOLD_LABELS_ONLY = """\
**Overall Score:** **8.2 / 10.0** (Strong / Sound)
* **Rigor:** 2.9/3.5 | **Claims:** 2.0/2.5 | **Biology:** 1.8/2.0 | **Reproducibility:** 1.5/2.0
"""

JSON_BLOCK = """
```json
{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2, "total": 7.2, "core": 6.0, "tier": "Strong / Sound"}
```
"""


def test_json_block_preferred_and_consistent_header_is_valid():
    scores = parse_scores(HEADER_BOLD + "body text\n" + JSON_BLOCK)
    assert scores.source == "json"
    assert (scores.rigor, scores.claims, scores.biology, scores.reproducibility) == (2.6, 1.8, 1.6, 1.2)
    assert (scores.total, scores.core, scores.tier) == (7.2, 6.0, "Strong / Sound")
    assert scores.issues == []


def test_last_json_block_wins_and_string_values_tolerated():
    early = '```json\n{"rigor": 0.1, "total": 0.1}\n```\n'
    late = ('```json\n{"rigor": "2.6/3.5", "claims": "1.8 / 2.5", "biology": "1.6", "reproducibility": 1.2,'
            ' "total": "7.2/10", "core": "6.0/8.0", "tier": "strong/sound"}\n```\n')
    scores = parse_scores(early + "text\n" + late)
    assert scores.source == "json"
    assert (scores.rigor, scores.claims, scores.biology, scores.total, scores.core) == (2.6, 1.8, 1.6, 7.2, 6.0)
    assert scores.issues == []  # tier comparison is case/whitespace-insensitive


def test_json_ignores_unrelated_trailing_json_block():
    trailing = '```json\n{"note": "unrelated"}\n```\n'
    scores = parse_scores(JSON_BLOCK + trailing)
    assert scores.total == 7.2 and scores.source == "json"


def test_json_with_trailing_comma_and_untagged_fence():
    text = '```\n{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2, "total": 7.2,\n' \
           ' "core": 6.0, "tier": "Strong / Sound",}\n```\n'
    scores = parse_scores(text)
    assert scores.source == "json"
    assert scores.issues == []


def test_json_inside_markdown_fence_is_found():
    text = "```markdown\n" + HEADER_BOLD + "```\n" + JSON_BLOCK
    scores = parse_scores(text)
    assert scores.source == "json" and scores.issues == []


def test_undecodable_json_falls_back_to_markdown():
    text = HEADER_BOLD + '```json\n{"rigor": 2.6, "total": 7.2 "core": }\n```\n'
    scores = parse_scores(text)
    assert scores.source == "markdown"
    assert scores.total == 7.2
    assert "json score block could not be decoded" in scores.issues


def test_markdown_fallback_bold_header():
    scores = parse_scores(HEADER_BOLD)
    assert scores.source == "markdown"
    assert (scores.total, scores.core, scores.tier) == (7.2, 6.0, "Strong / Sound")
    assert scores.rigor == 2.6 and scores.reproducibility == 1.2
    assert scores.issues == []


def test_markdown_fallback_long_labels_unbolded():
    scores = parse_scores(HEADER_LONG_LABELS)
    assert (scores.rigor, scores.claims, scores.biology, scores.reproducibility) == (2.2, 1.7, 1.7, 1.4)
    assert (scores.total, scores.core, scores.tier) == (7.0, 5.6, "Strong / Sound")
    assert scores.issues == []


def test_markdown_fallback_pilot_variant_uses_first_score():
    scores = parse_scores(HEADER_PILOT)
    assert scores.total == 2.5
    assert scores.tier == "Fundamentally Invalid"
    assert (scores.rigor, scores.claims, scores.biology, scores.reproducibility) == (0.5, 0.4, 0.5, 1.1)
    assert scores.issues == ["missing core"]


def test_markdown_bold_labels():
    scores = parse_scores(HEADER_BOLD_LABELS_ONLY)
    assert (scores.rigor, scores.claims, scores.biology, scores.reproducibility) == (2.9, 2.0, 1.8, 1.5)
    assert scores.total == 8.2
    assert scores.issues == ["missing core"]


def test_composite_score_line_is_accepted():
    text = "**Composite Score:** 7.8 / 10.0 (Strong / Sound)\nRigor: 2.6/3.5 | Claims: 1.9/2.5 | " \
           "Biology: 1.7/2.0 | Reproducibility: 1.6/2.0\n**Scientific Core Score:** 6.2 / 8.0\n"
    scores = parse_scores(text)
    assert scores.total == 7.8 and scores.core == 6.2 and scores.issues == []


def test_template_placeholders_do_not_parse():
    text = ("**Overall Score:** **[X.X] / 10.0** ([Tier Name]) | **Scientific Core Score:** **[X.X] / 8.0**\n"
            "* **Score Breakdown:** Rigor: [X.X]/3.5 | Claims: [X.X]/2.5 | Biology: [X.X]/2.0 | "
            "Reproducibility: [X.X]/2.0\n")
    scores = parse_scores(text)
    assert scores.source == "none"
    assert scores.issues and scores.total is None


def test_no_scores():
    scores = parse_scores("The model refused to score this manuscript.")
    assert scores.source == "none"
    assert scores.issues == ["no scores found (neither a ```json block nor a score header)"]


def test_missing_fields_reported():
    scores = parse_scores('```json\n{"total": 7.2, "tier": "Strong / Sound"}\n```')
    assert "missing rigor" in scores.issues
    assert "missing core" in scores.issues


def test_out_of_range():
    scores = parse_scores('```json\n{"rigor": 3.8, "claims": 1.0, "biology": 1.0, "reproducibility": 1.0,'
                          ' "total": 6.8, "core": 5.8, "tier": "Mixed / Salvageable"}\n```')
    assert scores.issues == ["rigor 3.8 out of range 0-3.5"]


def test_total_and_core_arithmetic():
    scores = parse_scores('```json\n{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2,'
                          ' "total": 7.5, "core": 6.3, "tier": "Strong / Sound"}\n```')
    assert any(issue.startswith("total 7.5 != sum") for issue in scores.issues)
    assert any(issue.startswith("core 6.3 != rigor+claims+biology") for issue in scores.issues)


def test_rounding_within_tolerance_is_accepted():
    scores = parse_scores('```json\n{"rigor": 2.62, "claims": 1.85, "biology": 1.6, "reproducibility": 1.2,'
                          ' "total": 7.3, "core": 6.1, "tier": "Strong / Sound"}\n```')
    assert scores.issues == []


def test_tier_inconsistent_with_total():
    scores = parse_scores('```json\n{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2,'
                          ' "total": 7.2, "core": 6.0, "tier": "Exemplary"}\n```')
    assert scores.issues == ["tier 'Exemplary' inconsistent with total 7.2 (expected 'Strong / Sound')"]


def test_json_markdown_disagreement():
    header = HEADER_BOLD.replace("**7.2 / 10.0**", "**7.6 / 10.0**")
    scores = parse_scores(header + JSON_BLOCK)
    assert scores.source == "json"
    assert scores.total == 7.2
    assert scores.issues == ["json total 7.2 disagrees with markdown header 7.6"]


def test_json_missing_field_filled_from_markdown():
    block = ('```json\n{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2, "total": 7.2,'
             ' "tier": "Strong / Sound"}\n```\n')
    scores = parse_scores(HEADER_BOLD + block)
    assert scores.core == 6.0
    assert scores.issues == []


@pytest.mark.parametrize("total, tier", [
    (10.0, "Exemplary"), (9.0, "Exemplary"), (8.9, "Strong / Sound"), (7.0, "Strong / Sound"),
    (6.9, "Mixed / Salvageable"), (5.0, "Mixed / Salvageable"), (4.9, "Flawed / Overstated"),
    (3.0, "Flawed / Overstated"), (2.9, "Fundamentally Invalid"), (0.0, "Fundamentally Invalid"),
])
def test_tier_for(total, tier):
    assert tier_for(total) == tier


@pytest.mark.parametrize("rigor, total, core", [(2.25, 8.3, 6.3), (2.25, 8.2, 6.2), (2.75, 8.8, 6.8)])
def test_rounding_exactly_at_the_tolerance_is_accepted(rigor, total, core):
    tier = "Strong / Sound"
    text = (f"**Overall Score:** **{total} / 10.0** ({tier}) | **Scientific Core Score:** **{core} / 8.0**\n"
            f"Rigor: {rigor}/3.5 | Claims: 2.0/2.5 | Biology: 2.0/2.0 | Reproducibility: 2.0/2.0\n"
            f'```json\n{{"rigor": {rigor}, "claims": 2.0, "biology": 2.0, "reproducibility": 2.0, '
            f'"total": {total}, "core": {core}, "tier": "{tier}"}}\n```\n')
    assert parse_scores(text).issues == []


def test_score_object_followed_by_stray_text_in_the_fence():
    text = ('```json\n{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2, "total": 7.2, '
            '"core": 6.0, "tier": "Strong / Sound"}\nEnd of review.\n```\n')
    scores = parse_scores(text)
    assert scores.source == "json" and scores.total == 7.2 and scores.issues == []


@pytest.mark.parametrize("tier", ["Strong / Sound (7.0\u20138.9)", "Strong / Sound (7.0-8.9)", "(Strong / Sound)"])
def test_tier_with_a_range_in_brackets(tier):
    text = HEADER_BOLD + ('```json\n{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2, '
                          f'"total": 7.2, "core": 6.0, "tier": "{tier}"}}\n```\n')
    scores = parse_scores(text)
    assert scores.tier == "Strong / Sound" and scores.issues == []

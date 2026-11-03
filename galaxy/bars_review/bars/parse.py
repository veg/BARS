"""Extract and validate BARS scores from a review in REVIEW_TEMPLATE.md layout.

The trailing fenced ```json block is preferred; the markdown score header is the
fallback and is also cross-checked against the JSON. Problems are reported in
Scores.issues rather than raised.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .model import Scores

FIELDS = ("rigor", "claims", "biology", "reproducibility", "total", "core")
MAXIMA = {"rigor": 3.5, "claims": 2.5, "biology": 2.0, "reproducibility": 2.0, "total": 10.0, "core": 8.0}
TOLERANCE = 0.05
_EPSILON = 1e-9  # 8.3 - 8.25 is 0.0500000000000007 in floating point: exactly at the tolerance

# (lower bound of total, tier name), highest first; mirrors the template's tier table.
TIERS = (
    (9.0, "Exemplary"),
    (7.0, "Strong / Sound"),
    (5.0, "Mixed / Salvageable"),
    (3.0, "Flawed / Overstated"),
    (float("-inf"), "Fundamentally Invalid"),
)

_NUM = r"(\d+(?:\.\d+)?)"
_SEP = r"[\s*:`_]*"  # bold markers, colons and spaces between a label and its value
_SLASH = r"[\s*`]*/[\s*`]*"
_DENOMINATORS = {
    "rigor": r"3\.5",
    "claims": r"2\.5",
    "biology": r"2(?:\.0+)?",
    "reproducibility": r"2(?:\.0+)?",
}
_LABELS = {
    "rigor": r"(?:Methodological(?:\s*(?:&|and)\s*Statistical)?\s+)?Rigou?r",
    "claims": r"Claims?(?:[\s-]+(?:to[\s-]+|vs\.?\s+)?Evidence(?:\s+Alignment)?)?",
    "biology": r"(?:Data\s+Integrity\s*(?:&|and)\s*)?Biolog(?:y|ical\s+Plausibility)",
    "reproducibility": r"Reproducibility(?:\s*(?:&|and)\s*Data\s+Integrity)?",
}
_BREAKDOWN_RES = {
    name: re.compile(rf"(?<![\w-])(?:{label}){_SEP}{_NUM}{_SLASH}{_DENOMINATORS[name]}(?!\.?\d)", re.I)
    for name, label in _LABELS.items()
}
_OVERALL_RES = tuple(
    re.compile(rf"{label}\s+Score{_SEP}{_NUM}{_SLASH}10(?:\.0+)?(?!\.?\d)[\s*`]*(?:\(([^)\n]*)\))?", re.I)
    for label in (r"Overall", r"(?:Total\s+)?Composite")
)
_CORE_RE = re.compile(rf"Scientific\s+Core(?:\s+Score)?{_SEP}{_NUM}{_SLASH}8(?:\.0+)?(?!\.?\d)", re.I)
_JSON_FENCE_RE = re.compile(r"```[ \t]*json[ \t]*\n(.*?)```", re.S | re.I)
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\"(?:total|rigor)\"[^{}]*\}", re.I)  # untagged fence or bare JSON
_JSON_KEYS = {
    "rigor": ("rigor", "methodological_rigor", "rigour"),
    "claims": ("claims", "claim_evidence_alignment", "claims_evidence"),
    "biology": ("biology", "biological_plausibility"),
    "reproducibility": ("reproducibility",),
    "total": ("total", "overall", "overall_score", "total_score"),
    "core": ("core", "core_score", "scientific_core", "scientific_core_score"),
}


def parse_scores(markdown: str) -> Scores:
    """Parse the review's scores; Scores.issues lists every validation problem (empty = valid)."""
    issues: list[str] = []
    from_json = _from_json(markdown, issues)
    from_md = _from_markdown(markdown)

    if from_json is not None:
        issues.extend(_disagreements(from_json, from_md))
        scores = from_json
        scores.source = "json"
        _fill_missing(scores, from_md)
    elif _has_any(from_md):
        scores = from_md
        scores.source = "markdown"
    else:
        scores = Scores(source="none")
        issues.append("no scores found (neither a ```json block nor a score header)")
        scores.issues = issues
        return scores

    issues.extend(validate(scores))
    scores.issues = issues
    return scores


def validate(scores: Scores) -> list[str]:
    """Range, arithmetic and tier-consistency checks for one set of scores."""
    issues = []
    for name in FIELDS:
        value = getattr(scores, name)
        if value is None:
            issues.append(f"missing {name}")
        elif not 0 <= value <= MAXIMA[name]:
            issues.append(f"{name} {value:g} out of range 0-{MAXIMA[name]:g}")
    if not scores.tier:
        issues.append("missing tier")

    parts = [scores.rigor, scores.claims, scores.biology, scores.reproducibility]
    if scores.total is not None and None not in parts:
        expected = sum(parts)
        if abs(scores.total - expected) > TOLERANCE + _EPSILON:
            issues.append(f"total {scores.total:g} != sum of sub-scores {expected:.2f}")
    if scores.core is not None and None not in parts[:3]:
        expected = sum(parts[:3])
        if abs(scores.core - expected) > TOLERANCE + _EPSILON:
            issues.append(f"core {scores.core:g} != rigor+claims+biology {expected:.2f}")
    if scores.tier and scores.total is not None:
        expected_tier = tier_for(scores.total)
        if _norm_tier(scores.tier) != _norm_tier(expected_tier):
            issues.append(f"tier '{scores.tier}' inconsistent with total {scores.total:g} (expected '{expected_tier}')")
    return issues


def tier_for(total: float) -> str:
    for lower, name in TIERS:
        if total >= lower:
            return name
    return TIERS[-1][1]


def _from_json(markdown: str, issues: list[str]) -> Scores | None:
    """Scores from the last ```json block holding score keys; else the last bare score object."""
    tagged = [m.group(1) for m in _JSON_FENCE_RE.finditer(markdown)]
    bare = [m.group(0) for m in _JSON_OBJECT_RE.finditer(markdown)]
    for bodies in (tagged, bare):
        for body in reversed(bodies):
            data = _load_json(body)
            if data is None and bodies is tagged and (inner := _JSON_OBJECT_RE.search(body)):
                data = _load_json(inner.group(0))  # the score object followed by stray text in the fence
            if data is None:
                if bodies is tagged and re.search(r'"(?:total|rigor)"', body):
                    issues.append("json score block could not be decoded")
                continue
            scores = _scores_from_mapping(data)
            if scores is not None:
                return scores
    return None


def _load_json(body: str) -> dict[str, Any] | None:
    for candidate in (body, re.sub(r",\s*([}\]])", r"\1", body)):  # second try: drop trailing commas
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        return data if isinstance(data, dict) else None
    return None


def _scores_from_mapping(data: dict[str, Any]) -> Scores | None:
    lowered = {re.sub(r"[\s-]+", "_", str(k).strip().lower()): v for k, v in data.items()}
    scores = Scores()
    for name, keys in _JSON_KEYS.items():
        for key in keys:
            if key in lowered:
                setattr(scores, name, _number(lowered[key]))
                break
    tier = lowered.get("tier")
    if isinstance(tier, str):
        scores.tier = _clean_tier(tier)
    return scores if _has_any(scores) else None


def _number(value: Any) -> float | None:
    """Accept 2.5, "2.5" or "2.5/3.5" (the numerator is the score)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            return float(match.group())
    return None


def _from_markdown(markdown: str) -> Scores:
    scores = Scores()
    for regex in _OVERALL_RES:
        match = regex.search(markdown)
        if match:
            scores.total = float(match.group(1))
            scores.tier = _clean_tier(match.group(2) or "")
            break
    match = _CORE_RE.search(markdown)
    if match:
        scores.core = float(match.group(1))
    for name, regex in _BREAKDOWN_RES.items():
        match = regex.search(markdown)
        if match:
            setattr(scores, name, float(match.group(1)))
    return scores


def _clean_tier(text: str) -> str:
    """'**Strong / Sound** — note; more' or 'Strong / Sound (7.0–8.9)' -> 'Strong / Sound'."""
    text = re.sub(r"[*`_]", "", text).strip().strip("()")
    text = re.sub(r"(?<=\S)\s*\(.*$", "", text)  # a score range or note in brackets
    text = re.split(r"\s[—–-]\s|[—–;,|]", text, maxsplit=1)[0]
    text = re.sub(r"(?i)^\s*tier\s*:\s*", "", text)
    return " ".join(text.split())


def _norm_tier(tier: str) -> str:
    return re.sub(r"\s+", "", tier).lower()


def _has_any(scores: Scores) -> bool:
    return any(getattr(scores, name) is not None for name in FIELDS) or bool(scores.tier)


def _fill_missing(scores: Scores, fallback: Scores) -> None:
    for name in FIELDS:
        if getattr(scores, name) is None:
            setattr(scores, name, getattr(fallback, name))
    if not scores.tier:
        scores.tier = fallback.tier


def _disagreements(from_json: Scores, from_md: Scores) -> list[str]:
    issues = []
    for name in FIELDS:
        a, b = getattr(from_json, name), getattr(from_md, name)
        if a is not None and b is not None and abs(a - b) > TOLERANCE + _EPSILON:
            issues.append(f"json {name} {a:g} disagrees with markdown header {b:g}")
    if from_json.tier and from_md.tier and _norm_tier(from_json.tier) != _norm_tier(from_md.tier):
        issues.append(f"json tier '{from_json.tier}' disagrees with markdown header '{from_md.tier}'")
    return issues

"""Shared data model for the BARS pipeline: ingest -> scrub -> leakcheck -> review -> parse."""

from __future__ import annotations

from dataclasses import dataclass, field

# Section kinds. Scrubbing drops DROP_KINDS; everything else is sent to the reviewer.
KINDS = ("abstract", "body", "availability", "back", "ack", "funding", "contrib", "coi", "refs")
DROP_KINDS = frozenset({"ack", "funding", "contrib", "coi", "refs"})

# Per-paper status values written to scores.tsv.
STATUSES = (
    "ok",                # reviewed, scores parsed and valid
    "scores_invalid",    # reviewed, but scores missing or inconsistent (see score_issues)
    "scrubbed",          # scrub-only mode: scrubbed and leak-checked, no review requested
    "not_open_access",   # identifier resolved but no open-access full text in Europe PMC
    "fetch_error",       # identifier could not be resolved or downloaded
    "ingest_error",      # input could not be parsed (bad XML/PDF)
    "leak_blocked",      # leak check found blocking identifiers; not sent to the reviewer
    "review_error",      # API call failed after retries
)


@dataclass
class Author:
    surname: str
    given: str = ""
    heuristic: bool = False  # True when guessed from PDF front matter rather than structured metadata


@dataclass
class Section:
    title: str
    text: str  # markdown body of the section, without its heading
    kind: str = "body"  # one of KINDS
    level: int = 2  # markdown heading level used when rendering


@dataclass
class Document:
    source_id: str  # identifier as supplied by the user (PMCID, PMID, DOI, or upload name)
    source_type: str  # "jats" or "pdf"
    title: str = ""
    authors: list[Author] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)  # full affiliation strings
    institutions: list[str] = field(default_factory=list)  # institution names (JATS <institution>, PDF guesses)
    emails: list[str] = field(default_factory=list)
    orcids: list[str] = field(default_factory=list)
    journal: str = ""
    publisher: str = ""
    year: int | None = None  # publication year t0; None when unknown
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    sections: list[Section] = field(default_factory=list)  # abstract first, then body/back in reading order
    front_matter: str = ""  # raw text removed as front matter (PDF), kept only for leak checking
    notes: list[str] = field(default_factory=list)  # ingest warnings (e.g. truncated tables)


@dataclass
class ScrubResult:
    text: str  # markdown sent to the reviewer
    counts: dict[str, int] = field(default_factory=dict)  # placeholder token -> replacements made
    dropped_sections: list[str] = field(default_factory=list)  # "kind: title" of removed sections


@dataclass
class Leak:
    kind: str  # e.g. email, orcid, url, doi, pmcid, author_name, surname, institution, domain, term, title
    severity: str  # "block" or "warn"
    match: str
    context: str  # up to ~80 chars around the match, newlines collapsed


@dataclass
class ReviewResult:
    markdown: str
    model: str  # model ID requested
    model_version: str = ""  # model version reported by the API
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)  # prompt/output/thinking/total token counts
    mock: bool = False
    attempts: int = 1
    seconds: float = 0.0


@dataclass
class Scores:
    rigor: float | None = None  # /3.5
    claims: float | None = None  # /2.5
    biology: float | None = None  # /2.0
    reproducibility: float | None = None  # /2.0
    total: float | None = None  # /10.0, as stated by the reviewer
    core: float | None = None  # /8.0, as stated by the reviewer
    tier: str = ""
    source: str = "none"  # "json", "markdown" or "none"
    issues: list[str] = field(default_factory=list)  # validation problems; empty means valid

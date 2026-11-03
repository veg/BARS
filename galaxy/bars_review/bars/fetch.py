"""Resolve PMCID/PMID/DOI identifiers and download open-access JATS XML from Europe PMC."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import unquote

import requests

from . import __version__

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
USER_AGENT = f"bars-review/{__version__} (Galaxy tool; +https://github.com/spond/gemini-reviews)"
TIMEOUT = 30
MAX_RETRIES = 3  # retries after the first attempt
BACKOFF = 2.0  # seconds; doubled on every retry
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

_sleep = time.sleep  # replaced in tests

_PMCID_RE = re.compile(r"^(?:pmcid:?\s*)?pmc\s*(\d+)$", re.I)
_PMID_RE = re.compile(r"^(?:pmid:?\s*)?(\d+)$", re.I)
_DOI_RE = re.compile(r"^(?:doi:?\s*)?(10\.\d+(?:\.\d+)*/\S+)$", re.I)
_DOI_URL_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/(10\.\S+)$", re.I)
_PMC_URL_RE = re.compile(
    r"(?:ncbi\.nlm\.nih\.gov/pmc/articles|pmc\.ncbi\.nlm\.nih\.gov/articles|europepmc\.org/(?:abstract|article)/pmc)"
    r"/(PMC\d+)", re.I)
_PUBMED_URL_RE = re.compile(r"(?:pubmed\.ncbi\.nlm\.nih\.gov|europepmc\.org/(?:abstract|article)/med)/(\d+)", re.I)


class FetchError(Exception):
    """An identifier could not be resolved or its full text could not be downloaded."""


class NotOpenAccess(FetchError):
    """The article exists but Europe PMC has no open-access full-text XML for it."""


def classify_id(s: str) -> tuple[str, str]:
    """Normalise a user identifier to ("pmcid", "PMC123"), ("pmid", "123") or ("doi", "10.x/y")."""
    text = s.strip().strip("\"'<>").strip()
    if not text:
        raise ValueError("empty identifier")
    if m := _PMC_URL_RE.search(text):
        return "pmcid", m.group(1).upper()
    if m := _PUBMED_URL_RE.search(text):
        return "pmid", m.group(1)
    if m := _DOI_URL_RE.match(text):
        return "doi", _clean_doi(m.group(1))
    if m := _PMCID_RE.match(text):
        return "pmcid", f"PMC{m.group(1)}"
    if m := _PMID_RE.match(text):
        return "pmid", m.group(1)
    if m := _DOI_RE.match(text):
        return "doi", _clean_doi(m.group(1))
    raise ValueError(f"not a PMCID, PMID or DOI: {text[:80]!r}")


def _clean_doi(doi: str) -> str:
    return unquote(doi).rstrip(".,;")


def resolve(kind: str, value: str, session: Any = None) -> dict:
    """Look an identifier up in Europe PMC and return its ids, year, title and open-access flags."""
    if kind == "pmcid":
        query = f"PMCID:{value}"
    elif kind == "pmid":
        query = f"EXT_ID:{value} AND SRC:MED"
    elif kind == "doi":
        query = f'DOI:"{value}"'
    else:
        raise ValueError(f"unknown identifier kind: {kind!r}")
    params = {"query": query, "format": "json", "resultType": "core"}
    response = _get(SEARCH_URL, params=params, session=session)
    try:
        results = response.json().get("resultList", {}).get("result", [])
    except (ValueError, AttributeError) as exc:
        raise FetchError(f"Europe PMC returned an unreadable search response for {kind} {value}") from exc
    if not results:
        raise FetchError(f"{kind} {value} not found in Europe PMC")
    hit = _best_hit(results, kind, value)
    pmcid = (hit.get("pmcid") or "").upper()
    year = hit.get("pubYear") or ""
    return {
        "pmcid": pmcid,
        "pmid": str(hit.get("pmid") or ""),
        "doi": hit.get("doi") or "",
        "year": int(year) if str(year).isdigit() else None,
        "is_open_access": hit.get("isOpenAccess") == "Y",
        "in_epmc": hit.get("inEPMC") == "Y",
        "title": re.sub(r"<[^>]+>", "", hit.get("title") or "").strip(),
    }


def _best_hit(results: list[dict], kind: str, value: str) -> dict:
    """Prefer the record matching the queried id, then one with a PMCID (full text)."""
    if kind == "doi":
        matching = [r for r in results if (r.get("doi") or "").lower() == value.lower()]
    elif kind == "pmcid":
        matching = [r for r in results if (r.get("pmcid") or "").upper() == value.upper()]
    else:
        matching = [r for r in results if str(r.get("pmid") or "") == value]
    candidates = matching or results
    with_pmc = [r for r in candidates if r.get("pmcid")]
    return (with_pmc or candidates)[0]


def fetch_jats(pmcid: str, session: Any = None) -> bytes:
    """Download the full-text JATS XML of an open-access article from Europe PMC.

    Europe PMC answers a request for full text it does not have with HTTP 404 or, nowadays, with
    HTTP 500 and a JSON error naming the fullTextXML path; both raise NotOpenAccess (the 500 only
    after one retry, in case it was a real server error).
    """
    kind, pmcid = classify_id(pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}")
    if kind != "pmcid":
        raise ValueError(f"not a PMCID: {pmcid!r}")
    response = _get(FULLTEXT_URL.format(pmcid=pmcid), session=session, not_found=NotOpenAccess,
                    missing_path="/fullTextXML")
    content = response.content or b""
    if not content.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        raise FetchError(f"Europe PMC returned no XML for {pmcid}")
    return content


def _get(url: str, params: dict | None = None, session: Any = None,
         not_found: type[FetchError] = FetchError, missing_path: str = "") -> requests.Response:
    """GET with timeout, User-Agent and retries (backoff) on 429/5xx and connection errors.

    A second HTTP 500 whose JSON error names a path ending in `missing_path` raises `not_found`.
    """
    http = session if session is not None else requests
    headers = {"User-Agent": USER_AGENT}
    problem, retry_after = "", ""
    missing = 0
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            _sleep(_retry_delay(attempt, retry_after))
        try:
            response = http.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as exc:
            problem, retry_after = type(exc).__name__, ""
            continue
        if response.status_code == 200:
            return response
        if response.status_code == 404:
            raise not_found(f"not found at Europe PMC (HTTP 404): {url}")
        if response.status_code == 500 and missing_path and _error_path(response).endswith(missing_path):
            missing += 1
            if missing > 1:
                raise not_found(f"no full text at Europe PMC (HTTP 500 for {missing_path.strip('/')}): {url}")
        if response.status_code not in RETRY_STATUS:
            raise FetchError(f"Europe PMC request failed (HTTP {response.status_code}): {url}")
        problem = f"HTTP {response.status_code}"
        retry_after = str((getattr(response, "headers", None) or {}).get("Retry-After", ""))
    raise FetchError(f"Europe PMC request failed after {MAX_RETRIES + 1} attempts ({problem}): {url}")


def _error_path(response: Any) -> str:
    """The 'path' of a JSON error body (Spring Boot style), else ''."""
    try:
        body = response.json()
    except (ValueError, AttributeError):
        return ""
    return str(body.get("path") or "") if isinstance(body, dict) else ""


def _retry_delay(attempt: int, retry_after: str) -> float:
    """Exponential backoff, stretched to a numeric Retry-After header (capped at 60 s)."""
    delay = BACKOFF * 2 ** (attempt - 1)
    if retry_after.isdigit():
        delay = max(delay, min(float(retry_after), 60.0))
    return delay

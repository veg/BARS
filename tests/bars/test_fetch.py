"""Tests for bars.fetch: identifier parsing and the Europe PMC client (HTTP is mocked, no network)."""

from __future__ import annotations

import pytest
import requests

from bars import __version__, fetch
from bars.fetch import FetchError, NotOpenAccess, classify_id, fetch_jats, resolve


class FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None, content: bytes = b"",
                 headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """Returns (or raises) the queued responses in order and records every call."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def slept(monkeypatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(fetch, "_sleep", delays.append)
    return delays


def hit(**overrides) -> dict:
    record = {"pmcid": "PMC9629619", "pmid": "36322581", "doi": "10.1371/journal.pone.0275623",
              "pubYear": "2022", "isOpenAccess": "Y", "inEPMC": "Y",
              "title": "RASCL: Rapid Assessment of Selection in <i>CLades</i>"}
    record.update(overrides)
    return record


def search(*hits: dict) -> FakeResponse:
    return FakeResponse(payload={"hitCount": len(hits), "resultList": {"result": list(hits)}})


XML = b'<?xml version="1.0"?>\n<article><front/></article>'


@pytest.mark.parametrize("raw, expected", [
    ("PMC9629619", ("pmcid", "PMC9629619")),
    ("pmc123", ("pmcid", "PMC123")),
    (" PMCID: PMC123 ", ("pmcid", "PMC123")),
    ("PMID:36322581", ("pmid", "36322581")),
    ("pmid 123", ("pmid", "123")),
    ("123", ("pmid", "123")),
    ("10.1/x", ("doi", "10.1/x")),
    ("10.1371/journal.pone.0275623", ("doi", "10.1371/journal.pone.0275623")),
    ("doi:10.1/x", ("doi", "10.1/x")),
    ("DOI: 10.1/x", ("doi", "10.1/x")),
    ("https://doi.org/10.1/x", ("doi", "10.1/x")),
    ("http://dx.doi.org/10.1371%2Fjournal.pone.0275623", ("doi", "10.1371/journal.pone.0275623")),
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC9629619/", ("pmcid", "PMC9629619")),
    ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9629619", ("pmcid", "PMC9629619")),
    ("https://pubmed.ncbi.nlm.nih.gov/36322581/", ("pmid", "36322581")),
])
def test_classify_id(raw, expected):
    assert classify_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "hello", "PMCX12", "PMC", "11.1/x", "doi:", "10.1371"])
def test_classify_id_rejects_garbage(raw):
    with pytest.raises(ValueError):
        classify_id(raw)


@pytest.mark.parametrize("kind, value, query", [
    ("pmcid", "PMC9629619", "PMCID:PMC9629619"),
    ("pmid", "36322581", "EXT_ID:36322581 AND SRC:MED"),
    ("doi", "10.1371/journal.pone.0275623", 'DOI:"10.1371/journal.pone.0275623"'),
])
def test_resolve_queries_europe_pmc(kind, value, query):
    session = FakeSession(search(hit()))
    resolve(kind, value, session=session)
    (call,) = session.calls
    assert call["url"] == fetch.SEARCH_URL
    assert call["params"] == {"query": query, "format": "json", "resultType": "core"}
    assert call["timeout"] == 30
    assert call["headers"]["User-Agent"] == fetch.USER_AGENT


def test_resolve_returns_normalised_record():
    meta = resolve("pmid", "36322581", session=FakeSession(search(hit(pmcid="pmc9629619"))))
    assert meta == {
        "pmcid": "PMC9629619", "pmid": "36322581", "doi": "10.1371/journal.pone.0275623", "year": 2022,
        "is_open_access": True, "in_epmc": True,
        "title": "RASCL: Rapid Assessment of Selection in CLades",
    }


def test_resolve_missing_fields_and_closed_access():
    meta = resolve("pmid", "1", session=FakeSession(search({"pmid": "1", "isOpenAccess": "N"})))
    assert meta["pmcid"] == "" and meta["doi"] == "" and meta["year"] is None
    assert meta["is_open_access"] is False and meta["in_epmc"] is False


def test_resolve_prefers_matching_doi_with_full_text():
    preprint = hit(pmcid="", pmid="", doi="10.1101/2022.01.15.476448")
    published = hit()
    meta = resolve("doi", "10.1371/JOURNAL.PONE.0275623", session=FakeSession(search(preprint, published)))
    assert meta["pmcid"] == "PMC9629619"


def test_resolve_not_found():
    with pytest.raises(FetchError, match="not found"):
        resolve("pmcid", "PMC1", session=FakeSession(search()))


def test_resolve_unreadable_json():
    with pytest.raises(FetchError, match="unreadable"):
        resolve("pmcid", "PMC1", session=FakeSession(FakeResponse(content=b"<html>")))


def test_resolve_unknown_kind():
    with pytest.raises(ValueError):
        resolve("isbn", "123", session=FakeSession())


def test_fetch_jats_downloads_xml():
    session = FakeSession(FakeResponse(content=XML))
    assert fetch_jats("PMC9629619", session=session) == XML
    assert session.calls[0]["url"] == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9629619/fullTextXML"


def test_fetch_jats_adds_pmc_prefix():
    session = FakeSession(FakeResponse(content=b"\xef\xbb\xbf  <article/>"))
    fetch_jats("9629619", session=session)
    assert "/PMC9629619/fullTextXML" in session.calls[0]["url"]


def test_fetch_jats_404_is_not_open_access():
    with pytest.raises(NotOpenAccess) as info:
        fetch_jats("PMC1", session=FakeSession(FakeResponse(status=404)))
    assert isinstance(info.value, FetchError)


def test_fetch_jats_rejects_non_xml():
    with pytest.raises(FetchError, match="no XML"):
        fetch_jats("PMC1", session=FakeSession(FakeResponse(content=b'{"error": "x"}')))


def test_retries_with_backoff_and_retry_after(slept):
    session = FakeSession(FakeResponse(status=503), FakeResponse(status=429, headers={"Retry-After": "7"}),
                          FakeResponse(content=XML))
    assert fetch_jats("PMC1", session=session) == XML
    assert len(session.calls) == 3
    assert slept == [2.0, 7.0]


def test_retries_connection_errors_and_timeouts():
    session = FakeSession(requests.ConnectionError("down"), requests.Timeout("slow"), FakeResponse(content=XML))
    assert fetch_jats("PMC1", session=session) == XML


def test_gives_up_after_three_retries(slept):
    session = FakeSession(*[FakeResponse(status=502) for _ in range(4)])
    with pytest.raises(FetchError, match="after 4 attempts"):
        fetch_jats("PMC1", session=session)
    assert len(session.calls) == fetch.MAX_RETRIES + 1 == 4
    assert slept == [2.0, 4.0, 8.0]


def test_client_errors_are_not_retried():
    session = FakeSession(FakeResponse(status=400), FakeResponse(content=XML))
    with pytest.raises(FetchError, match="HTTP 400"):
        fetch_jats("PMC1", session=session)
    assert len(session.calls) == 1


def missing_fulltext(pmcid: str = "PMC1") -> FakeResponse:
    """Europe PMC's current answer for a PMCID without full-text XML: HTTP 500 with a JSON error."""
    return FakeResponse(status=500, payload={"status": 500, "error": "Internal Server Error",
                                             "path": f"/europepmc/webservices/rest/{pmcid}/fullTextXML"})


def test_fetch_jats_json_500_for_the_fulltext_path_is_not_open_access(slept):
    session = FakeSession(missing_fulltext(), missing_fulltext())
    with pytest.raises(NotOpenAccess, match="no full text"):
        fetch_jats("PMC1", session=session)
    assert len(session.calls) == 2 and slept == [2.0]


def test_fetch_jats_retries_a_single_json_500():
    session = FakeSession(missing_fulltext(), FakeResponse(content=XML))
    assert fetch_jats("PMC1", session=session) == XML


def test_plain_500_is_still_a_transient_error(slept):
    session = FakeSession(*[FakeResponse(status=500) for _ in range(4)])
    with pytest.raises(FetchError, match="after 4 attempts") as info:
        fetch_jats("PMC1", session=session)
    assert not isinstance(info.value, NotOpenAccess)


def test_resolve_prefers_the_same_doi_record_with_a_pmcid():
    without, with_pmc = hit(pmcid="", isOpenAccess="N"), hit()
    meta = resolve("doi", "10.1371/journal.pone.0275623", session=FakeSession(search(without, with_pmc)))
    assert meta["pmcid"] == "PMC9629619" and meta["is_open_access"]


def test_default_transport_is_requests(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(content=XML)

    monkeypatch.setattr(fetch.requests, "get", fake_get)
    assert fetch_jats("PMC1") == XML
    assert calls


def test_user_agent():
    assert fetch.USER_AGENT == (f"bars-review/{__version__} (Galaxy tool; "
                                "+https://github.com/spond/gemini-reviews)")

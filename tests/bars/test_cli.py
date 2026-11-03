"""bars CLI: manifest handling, fatal errors, per-paper statuses, output files, exit codes.

Most tests replace the ingest/scrub/leakcheck modules seen by bars.cli with small fakes, so they
exercise only the CLI. Tests marked `needs_fixtures` run the real pipeline on the synthetic
test-data and are skipped until those fixtures exist. No network anywhere.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bars import cli, review
from bars.model import Author, Document, Leak, ReviewResult, ScrubResult, Section
from bars.review import ReviewError

REPO = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO / "galaxy" / "bars_review"
FIXTURE_JATS = TOOL_DIR / "test-data" / "synthetic_jats.xml"
FIXTURE_PDF = TOOL_DIR / "test-data" / "synthetic.pdf"
needs_fixtures = pytest.mark.skipif(
    not (FIXTURE_JATS.exists() and FIXTURE_PDF.exists()), reason="synthetic test-data fixtures not built yet")

FAKE_KEY = "AIzaFAKE-cli-test-key-9876543210"
SCORES_HEADER = ("id\tname\tinput_type\tstatus\ttotal\tcore\trigor\tclaims\tbiology\treproducibility\ttier\t"
                 "score_source\tscore_issues\tleaks_block\tleaks_warn\tmodel\tmodel_version\tprompt_tokens\t"
                 "output_tokens\tthinking_tokens\tseconds\tmessage")
LEAKS_HEADER = "id\tname\tkind\tseverity\tmatch\tcontext"
SCRUBBED_TEXT = "# [REDACTED FOR BLINDED REVIEW]\n\n## Abstract\n\nWe sequenced 40 marsupial genomes.\n"


# --- fakes ------------------------------------------------------------------


class FakeFetchError(Exception):
    pass


class FakeNotOpenAccess(FakeFetchError):  # as in bars.fetch: the except order in cli.ingest_id matters
    pass


def fake_classify(raw):
    raw = raw.strip()
    if re.fullmatch(r"(?i)pmc\d+", raw):
        return "pmcid", raw.upper()
    if re.fullmatch(r"(?i)(pmid:)?\d+", raw):
        return "pmid", raw.split(":")[-1]
    match = re.fullmatch(r"(?i)(?:doi:)?(10\.\d+/\S+)", raw)
    if match:
        return "doi", match.group(1)
    raise ValueError(f"unrecognised identifier {raw!r}")


BODY_TEXT = "Burrowing lineages were compared with codon models. " * 40  # more than cli.MIN_BODY_CHARS


def fake_doc(source_id, source_type="jats", sections=True):
    return Document(
        source_id=source_id, source_type=source_type, title=f"Secret Title of {source_id}",
        authors=[Author("Quokkason", "Alice")],
        sections=[Section("Abstract", "We sequenced 40 marsupial genomes.", kind="abstract"),
                  Section("Results", BODY_TEXT)] if sections else [],
        notes=["fake note"],
    )


@pytest.fixture
def fakes(monkeypatch):
    """Install fake ingest/scrub/leakcheck modules into bars.cli; returns a recorder."""
    state = SimpleNamespace(resolved=[], scrub_terms=[], check_terms=[], blocked=set(), docs=[], meta_year=None)

    def resolve(kind, value):
        state.resolved.append((kind, value))
        if value == "PMC404":
            raise FakeFetchError("Europe PMC returned 404")
        if value == "PMC403":
            return {"pmcid": "PMC403", "is_open_access": False, "in_epmc": False}
        if value == "PMC451":
            raise FakeNotOpenAccess("full text withheld")
        pmcid = value if kind == "pmcid" else "PMC999"
        return {"pmcid": pmcid, "pmid": "345", "doi": "10.1/x", "year": state.meta_year,
                "is_open_access": True, "in_epmc": True, "title": "Meta title"}

    def parse_jats(data, source_id):
        if data == b"not xml":
            raise ValueError("XML syntax error line 1")
        doc = fake_doc(source_id, sections=data != b"<article/>empty")
        state.docs.append(doc)
        return doc

    def scrub(doc, terms=()):
        state.scrub_terms.append(list(terms))
        return ScrubResult(text=SCRUBBED_TEXT, counts={"[EMAIL_REDACTED]": 1},
                           dropped_sections=["ack: Acknowledgements"])

    def check(text, doc, terms=()):
        state.check_terms.append(list(terms))
        leaks = [Leak("title", "warn", "marsupial genomes", "We sequenced 40\tmarsupial genomes.\n")]
        if doc.source_id in state.blocked:
            leaks.append(Leak("surname", "block", "Quokkason", "... Quokkason et al. ..."))
        return leaks

    monkeypatch.setattr(cli, "fetch", SimpleNamespace(
        classify_id=fake_classify, resolve=resolve, fetch_jats=lambda pmcid: b"<article/>",
        FetchError=FakeFetchError, NotOpenAccess=FakeNotOpenAccess))
    monkeypatch.setattr(cli, "jats", SimpleNamespace(parse_jats=parse_jats))
    monkeypatch.setattr(cli, "pdf", SimpleNamespace(parse_pdf=lambda data, sid: fake_doc(sid, "pdf")))
    monkeypatch.setattr(cli, "scrub", SimpleNamespace(scrub=scrub))
    monkeypatch.setattr(cli, "leakcheck", SimpleNamespace(
        check=check, blocking=lambda leaks: any(leak.severity == "block" for leak in leaks)))
    return state


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("BARS_MOCK", raising=False)
    monkeypatch.delenv("BARS_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("BARS_USER_GEMINI_API_KEY", raising=False)


def run_cli(tmp_path, lines, *extra, mode="scrub"):
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    code = cli.main(["run", "--manifest", str(manifest), "--outdir", str(out), "--mode", mode, *extra])
    return code, out


def read_tsv(path):
    header, *rows = path.read_text(encoding="utf-8").splitlines()
    columns = header.split("\t")
    return [dict(zip(columns, row.split("\t"), strict=True)) for row in rows]


def uploads(tmp_path):
    jats_file = tmp_path / "a.xml"
    jats_file.write_bytes(b"<article>fake</article>")
    pdf_file = tmp_path / "b.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake")
    return [f"jats\t{jats_file}\tMy Paper.xml", f"pdf\t{pdf_file}\t"]


# --- manifest and naming ----------------------------------------------------


def test_read_manifest(tmp_path):
    manifest = tmp_path / "m.tsv"
    manifest.write_text(
        "# a comment\n\n   \n"
        "id\tPMC1, PMC2  PMID: 345,doi: 10.1/abc\t\n"
        "ID\tdoi:10.1371/journal.pone.0123\n"
        "jats\t/data/a.xml\tPaper A\n"
        "pdf\t/data/b.pdf\t\n"
        "id\t\t\n",
        encoding="utf-8")
    items = cli.read_manifest(manifest)
    assert [(i.kind, i.value, i.label) for i in items] == [
        ("id", "PMC1", ""), ("id", "PMC2", ""), ("id", "PMID:345", ""), ("id", "doi:10.1/abc", ""),
        ("id", "doi:10.1371/journal.pone.0123", ""), ("jats", "/data/a.xml", "Paper A"), ("pdf", "/data/b.pdf", ""),
    ]


def test_plan_papers_names_and_anon_ids(fakes):
    items = [cli.InputItem("id", v) for v in ("pmc1", "PMC1", "PMID:345", "doi:10.1371/journal.pone.0123", "bogus!")]
    items += [cli.InputItem("jats", "/x/a.xml", "My Paper.xml"), cli.InputItem("pdf", "/x/My Paper.PDF", ""),
              cli.InputItem("pdf", "/x/c.pdf", "../../etc/passwd")]
    papers = cli.plan_papers(items)
    assert [p.id for p in papers] == ["PMC1", "PMC1", "PMID:345", "doi:10.1371/journal.pone.0123", "bogus!",
                                      "My Paper.xml", "My Paper.PDF", "../../etc/passwd"]
    assert [p.name for p in papers] == ["PMC1", "PMC1_2", "PMID_345", "doi_10.1371_journal.pone.0123", "bogus",
                                        "My_Paper", "My_Paper_2", "etc_passwd"]
    assert [p.input_type for p in papers] == ["pmcid", "pmcid", "pmid", "doi", "unknown", "jats", "pdf", "pdf"]
    assert [p.anon_id for p in papers] == [f"MANUSCRIPT_{i:03d}" for i in range(1, 9)]


def test_unique_name_is_case_insensitive_and_never_empty():
    taken = set()
    assert cli.unique_name("Paper", taken) == "Paper"
    assert cli.unique_name("paper", taken) == "paper_2"
    assert cli.unique_name("///", taken) == "paper_3"


def test_read_terms(tmp_path):
    path = tmp_path / "terms.txt"
    path.write_text("Foo  Bar, Baz\n\nQux\nBaz\nx\n", encoding="utf-8")
    assert cli.read_terms(path) == ["Foo Bar", "Baz", "Qux"]


# --- fatal errors (exit 2) --------------------------------------------------


def assert_fatal(code, capsys, fragment):
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("bars: error: ") and err.count("\n") == 1
    assert fragment in err


def test_unknown_kind_is_fatal(tmp_path, fakes, capsys):
    code, _ = run_cli(tmp_path, ["url\thttps://example.org/paper"])
    assert_fatal(code, capsys, "unknown kind 'url'")


def test_zero_inputs_is_fatal(tmp_path, fakes, capsys):
    code, _ = run_cli(tmp_path, ["# nothing here", ""])
    assert_fatal(code, capsys, "no inputs")


def test_too_many_inputs_is_fatal_before_network(tmp_path, fakes, capsys):
    ids = ", ".join(f"PMC{i}" for i in range(1, 27))
    code, out = run_cli(tmp_path, [f"id\t{ids}"], "--max-papers", "25")
    assert_fatal(code, capsys, "26 papers given, at most 25")
    assert fakes.resolved == []
    assert not (out / "scores.tsv").exists()


def test_uploads_require_confirmation(tmp_path, fakes, capsys):
    code, _ = run_cli(tmp_path, uploads(tmp_path))
    assert_fatal(code, capsys, "--confirm-not-confidential")


def test_review_mode_requires_key_or_mock(tmp_path, fakes, capsys, monkeypatch):
    code, _ = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert_fatal(code, capsys, "BARS_USER_GEMINI_API_KEY")
    assert fakes.resolved == []

    monkeypatch.setenv("BARS_MOCK", "1")
    code, _ = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 0


def test_missing_manifest_is_fatal(tmp_path, capsys):
    code = cli.main(["run", "--manifest", str(tmp_path / "nope.tsv"), "--outdir", str(tmp_path / "o")])
    assert_fatal(code, capsys, "cannot read manifest")


def test_unreadable_terms_file_is_fatal(tmp_path, fakes, capsys):
    code, _ = run_cli(tmp_path, ["id\tPMC1"], "--redact-terms", str(tmp_path / "missing.txt"))
    assert_fatal(code, capsys, "cannot read redact-terms file")


def test_usage_errors_return_2(capsys):
    assert cli.main([]) == 2
    assert cli.main(["run", "--manifest", "m.tsv"]) == 2
    assert cli.main(["run", "--manifest", "m", "--outdir", "o", "--mode", "bogus"]) == 2


# --- per-paper processing ---------------------------------------------------


def test_scrub_mode_outputs(tmp_path, fakes, capsys):
    lines = ["id\tPMC1 PMID:345", "id\t10.1371/journal.pone.0123", *uploads(tmp_path)]
    code, out = run_cli(tmp_path, lines, "--confirm-not-confidential")
    assert code == 0

    scores_text = (out / "scores.tsv").read_text(encoding="utf-8")
    assert scores_text.splitlines()[0] == SCORES_HEADER
    rows = read_tsv(out / "scores.tsv")
    assert [r["name"] for r in rows] == ["PMC1", "PMID_345", "doi_10.1371_journal.pone.0123", "My_Paper", "b"]
    assert [r["input_type"] for r in rows] == ["pmcid", "pmid", "doi", "jats", "pdf"]
    assert {r["status"] for r in rows} == {"scrubbed"}
    assert all(r["total"] == "" and r["model"] == "" and r["leaks_warn"] == "1" for r in rows)

    for row in rows:
        assert (out / "scrubbed" / f"{row['name']}.md").read_text(encoding="utf-8") == SCRUBBED_TEXT
    assert list((out / "reviews").iterdir()) == []

    leak_lines = (out / "leaks.tsv").read_text(encoding="utf-8").splitlines()
    assert leak_lines[0] == LEAKS_HEADER
    assert leak_lines[1] == "PMC1\tPMC1\ttitle\twarn\tmarsupial genomes\tWe sequenced 40 marsupial genomes."
    assert len(leak_lines) == 6

    meta = json.loads((out / "run_metadata.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "scrub" and meta["tools"] == [] and meta["mock"] is False
    assert meta["package_version"] == "0.1.0"
    assert meta["template_sha256"] == review.template_sha256()
    assert meta["n_inputs"] == 5
    assert meta["status_counts"]["scrubbed"] == 5 and set(meta["status_counts"]) == set(cli.STATUSES)
    first = meta["papers"][0]
    assert first["anon_id"] == "MANUSCRIPT_001"
    assert first["redaction_counts"] == {"[EMAIL_REDACTED]": 1}
    assert first["dropped_sections"] == ["ack: Acknowledgements"]
    assert first["notes"] == ["fake note"]
    for key in ("started_utc", "finished_utc", "temperature", "max_output_tokens", "seed", "model"):
        assert key in meta

    stdout = capsys.readouterr().out
    assert "[1/5] PMC1: scrubbed" in stdout
    assert "We sequenced" not in stdout and "Secret Title" not in stdout


def test_failures_do_not_abort_the_batch(tmp_path, fakes):
    missing = tmp_path / "missing.pdf"
    bad = tmp_path / "bad.xml"
    bad.write_bytes(b"not xml")
    empty = tmp_path / "empty.xml"
    empty.write_bytes(b"<article/>empty")
    lines = ["id\tPMC404, PMC403, PMC451, bogus!, PMC1", f"pdf\t{missing}\tgone.pdf", f"jats\t{bad}\tbad.xml",
             f"jats\t{empty}\tempty.xml"]
    code, out = run_cli(tmp_path, lines, "--confirm-not-confidential")
    assert code == 0
    rows = {r["name"]: r for r in read_tsv(out / "scores.tsv")}
    assert rows["PMC404"]["status"] == "fetch_error" and "404" in rows["PMC404"]["message"]
    assert rows["PMC403"]["status"] == "not_open_access"
    assert rows["PMC451"]["status"] == "not_open_access" and "withheld" in rows["PMC451"]["message"]
    assert rows["bogus"]["status"] == "fetch_error" and rows["bogus"]["input_type"] == "unknown"
    assert rows["PMC1"]["status"] == "scrubbed"
    assert rows["gone"]["status"] == "ingest_error"
    assert rows["bad"]["status"] == "ingest_error" and "XML syntax error" in rows["bad"]["message"]
    assert rows["empty"]["status"] == "ingest_error" and "no text sections" in rows["empty"]["message"]
    assert sorted(p.name for p in (out / "scrubbed").iterdir()) == ["PMC1.md"]


def test_metadata_fills_missing_year(tmp_path, fakes):
    fakes.meta_year = 2019
    code, _ = run_cli(tmp_path, ["id\tPMC1"])
    assert code == 0
    assert fakes.docs[0].year == 2019
    assert fakes.docs[0].pmid == "345"


def test_exit_1_when_nothing_succeeds(tmp_path, fakes, capsys):
    code, out = run_cli(tmp_path, ["id\tPMC404"])
    assert code == 1
    assert "no paper was processed successfully" in capsys.readouterr().err
    assert read_tsv(out / "scores.tsv")[0]["status"] == "fetch_error"
    assert json.loads((out / "run_metadata.json").read_text())["status_counts"]["fetch_error"] == 1


def test_leak_blocked_paper_is_not_reviewed(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("BARS_MOCK", "1")
    sent = []
    real_review = review.review
    monkeypatch.setattr(cli.review, "review", lambda text, anon_id, *a, **k: sent.append(anon_id) or
                        real_review(text, anon_id, *a, **k))
    fakes.blocked = {"PMC2"}
    code, out = run_cli(tmp_path, ["id\tPMC1 PMC2"], mode="review")
    assert code == 0
    rows = {r["name"]: r for r in read_tsv(out / "scores.tsv")}
    assert rows["PMC2"]["status"] == "leak_blocked"
    assert rows["PMC2"]["leaks_block"] == "1" and "surname" in rows["PMC2"]["message"]
    assert (out / "scrubbed" / "PMC2.md").exists()
    assert not (out / "reviews" / "PMC2.md").exists()
    assert sent == ["MANUSCRIPT_001"]
    assert "PMC2\tPMC2\tsurname\tblock\tQuokkason" in (out / "leaks.tsv").read_text()


def test_all_blocked_exit_1(tmp_path, fakes):
    fakes.blocked = {"PMC1"}
    code, _ = run_cli(tmp_path, ["id\tPMC1"])
    assert code == 1


def test_review_mode_with_mock(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("BARS_MOCK", "1")
    calls = []
    real_review = review.review

    def spy(text, anon_id, model, **kwargs):
        calls.append((text, anon_id, model, kwargs))
        return real_review(text, anon_id, model, **kwargs)

    monkeypatch.setattr(cli.review, "review", spy)
    code, out = run_cli(tmp_path, ["id\tPMC1"], "--model", "gemini-x", "--temperature", "0.7", "--seed", "3",
                        mode="review")
    assert code == 0
    (text, anon_id, model, kwargs) = calls[0]
    assert text == SCRUBBED_TEXT and anon_id == "MANUSCRIPT_001" and model == "gemini-x"
    assert kwargs == {"temperature": 0.7, "max_output_tokens": 32768, "seed": 3}

    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "ok"
    assert (row["total"], row["core"], row["rigor"], row["claims"], row["biology"], row["reproducibility"]) == \
        ("7.2", "6.0", "2.6", "1.8", "1.6", "1.2")
    assert row["tier"] == "Strong / Sound" and row["score_source"] == "json" and row["score_issues"] == ""
    assert row["model"] == "gemini-x" and row["model_version"] == "mock"

    text = (out / "reviews" / "PMC1.md").read_text(encoding="utf-8")
    assert text.startswith("# Review of MANUSCRIPT_001")
    footer = text.split("**BARS provenance**", 1)[1]
    for expected in ("Source: PMC1", "Title (restored after review): Secret Title of PMC1", "Model requested: gemini-x",
                     "Model version: mock", review.template_sha256(), "BARS version: 0.1.0", "Mock review: yes",
                     "Generated (UTC): "):
        assert expected in footer
    assert "Secret Title" not in text.split("**BARS provenance**", 1)[0]

    meta = json.loads((out / "run_metadata.json").read_text())
    assert meta["mock"] is True and meta["model"] == "gemini-x" and meta["seed"] == 3 and meta["temperature"] == 0.7
    assert meta["papers"][0]["model_version"] == "mock"
    assert meta["api_key_source"] == "mock"


def test_review_error_status(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("BARS_GEMINI_API_KEY", FAKE_KEY)

    def failing(*args, **kwargs):
        raise ReviewError("Gemini API call failed after 5 attempt(s): ServerError: 503 UNAVAILABLE", attempts=5)

    monkeypatch.setattr(cli.review, "review", failing)
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "review_error" and "503" in row["message"]
    assert list((out / "reviews").iterdir()) == []
    assert (out / "scrubbed" / "PMC1.md").exists()


def test_scores_invalid_status(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("BARS_GEMINI_API_KEY", FAKE_KEY)
    markdown = ('# Review of MANUSCRIPT_001\n\n```json\n{"rigor": 2.6, "claims": 1.8, "biology": 1.6, '
                '"reproducibility": 1.2, "total": 9.5, "core": 6.0, "tier": "Exemplary"}\n```\n')
    monkeypatch.setattr(cli.review, "review", lambda *a, **k: ReviewResult(
        markdown=markdown, model="gemini-x", model_version="gemini-x-002", finish_reason="STOP",
        usage={"prompt_token_count": 11, "candidates_token_count": 22, "thoughts_token_count": 33}))
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 0
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "scores_invalid"
    assert row["score_issues"].startswith("total 9.5 != sum of sub-scores")
    assert (row["prompt_tokens"], row["output_tokens"], row["thinking_tokens"]) == ("11", "22", "33")
    assert (out / "reviews" / "PMC1.md").exists()


def test_key_never_reaches_outputs(tmp_path, fakes, monkeypatch, capsys):
    monkeypatch.setenv("BARS_GEMINI_API_KEY", FAKE_KEY)

    def leaky(*args, **kwargs):  # simulate a bug that puts the key into an error message
        raise RuntimeError(f"request to ...?key={FAKE_KEY} failed")

    monkeypatch.setattr(cli.review, "review", leaky)
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "review_error" and "[API_KEY]" in row["message"]
    for path in out.rglob("*"):
        if path.is_file():
            assert FAKE_KEY not in path.read_text(encoding="utf-8"), path
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err


USER_KEY = "AIzaUSER-cli-test-key-0123456789"


@pytest.mark.parametrize(("envs", "source"), [
    ({"BARS_USER_GEMINI_API_KEY": USER_KEY}, "user"),
    ({"BARS_GEMINI_API_KEY": FAKE_KEY}, "server"),
    ({"BARS_USER_GEMINI_API_KEY": USER_KEY, "BARS_GEMINI_API_KEY": FAKE_KEY}, "user"),
])
def test_either_key_enables_review_and_its_source_is_recorded(tmp_path, fakes, monkeypatch, envs, source):
    for env, value in envs.items():
        monkeypatch.setenv(env, value)
    monkeypatch.setattr(cli.review, "review", lambda *a, **k: ReviewResult(
        markdown="# Review of MANUSCRIPT_001\n", model="gemini-x", model_version="gemini-x-002", finish_reason="STOP"))
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 0
    meta = json.loads((out / "run_metadata.json").read_text())
    assert meta["api_key_source"] == source
    for path in out.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert USER_KEY not in text and FAKE_KEY not in text, path


def test_scrub_mode_records_no_key(tmp_path, fakes):
    code, out = run_cli(tmp_path, ["id\tPMC1"])
    assert code == 0
    assert json.loads((out / "run_metadata.json").read_text())["api_key_source"] == "none"


def test_both_keys_are_redacted(tmp_path, fakes, monkeypatch, capsys):
    monkeypatch.setenv("BARS_USER_GEMINI_API_KEY", USER_KEY)
    monkeypatch.setenv("BARS_GEMINI_API_KEY", FAKE_KEY)

    def leaky(*args, **kwargs):  # simulate a bug that puts both keys into an error message
        raise RuntimeError(f"tried ?key={USER_KEY} then ?key={FAKE_KEY}")

    monkeypatch.setattr(cli.review, "review", leaky)
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["message"].count("[API_KEY]") == 2
    for path in out.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert USER_KEY not in text and FAKE_KEY not in text, path
    captured = capsys.readouterr()
    assert USER_KEY not in captured.out + captured.err and FAKE_KEY not in captured.out + captured.err


def test_redact_terms_reach_scrub_and_leakcheck(tmp_path, fakes):
    terms = tmp_path / "terms.txt"
    terms.write_text("Wombat Lab, Project Koala\nGrant 123\n", encoding="utf-8")
    code, out = run_cli(tmp_path, ["id\tPMC1"], "--redact-terms", str(terms))
    assert code == 0
    expected = ["Wombat Lab", "Project Koala", "Grant 123"]
    assert fakes.scrub_terms == [expected] and fakes.check_terms == [expected]
    meta = json.loads((out / "run_metadata.json").read_text())
    assert meta["n_redact_terms"] == 3
    assert "Wombat" not in (out / "run_metadata.json").read_text()


def test_scrub_crash_and_leakcheck_crash_are_contained(tmp_path, fakes, monkeypatch):
    def broken_scrub(doc, terms=()):
        raise RuntimeError("scrubber bug")

    monkeypatch.setattr(cli, "scrub", SimpleNamespace(scrub=broken_scrub))
    code, out = run_cli(tmp_path, ["id\tPMC1"])
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "ingest_error" and "scrubbing failed" in row["message"]

    monkeypatch.setattr(cli, "scrub", SimpleNamespace(scrub=lambda doc, terms=(): ScrubResult(text=SCRUBBED_TEXT)))

    def broken_check(text, doc, terms=()):
        raise RuntimeError("checker bug")

    monkeypatch.setattr(cli, "leakcheck", SimpleNamespace(check=broken_check, blocking=lambda leaks: False))
    code, out = run_cli(tmp_path, ["id\tPMC1"])
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "leak_blocked" and "leak check failed" in row["message"]


def test_tsv_cells_are_single_line():
    assert cli.tsv_cell("a\tb\nc\r\nd") == "a b c d"
    assert cli.tsv_cell(None) == ""
    assert cli.num(7.0) == "7.0" and cli.num(2.55) == "2.55" and cli.num(None) == ""


# --- real pipeline on the synthetic fixtures ----------------------------------

IDENTIFIERS = ("alice@quokka-lab.org", "quokka-lab.org", "0000-0002-1825-0097", "github.com/quokkalab",
               "usegalaxy.org", "/u/aquokka/", "workflowhub.eu", "10.5281/zenodo.1234567", "Quokkason",
               "Alice Quokkason", "University of Wombatia")


def fixture_manifest(tmp_path):
    return [f"jats\t{FIXTURE_JATS}\tsynthetic_jats.xml", f"pdf\t{FIXTURE_PDF}\tsynthetic.pdf"]


@needs_fixtures
def test_real_pipeline_scrub_mode(tmp_path):
    code, out = run_cli(tmp_path, fixture_manifest(tmp_path), "--confirm-not-confidential")
    rows = {r["name"]: r for r in read_tsv(out / "scores.tsv")}
    assert set(rows) == {"synthetic_jats", "synthetic"}
    assert {r["status"] for r in rows.values()} == {"scrubbed"}, rows
    assert code == 0
    assert all(r["leaks_block"] == "0" for r in rows.values())

    jats_md = (out / "scrubbed" / "synthetic_jats.md").read_text(encoding="utf-8")
    pdf_md = (out / "scrubbed" / "synthetic.md").read_text(encoding="utf-8")
    for text in (jats_md, pdf_md):
        assert text.startswith("# [REDACTED FOR BLINDED REVIEW]")
        assert "[CODE_REPO_URL]" in text
        for identifier in IDENTIFIERS:
            assert identifier not in text, identifier
    for placeholder in ("[GALAXY_HISTORY_URL]", "[DATA_ARCHIVE_DOI]", "[WORKFLOW_REGISTRY_URL]"):
        assert placeholder in jats_md, placeholder

    assert (out / "leaks.tsv").read_text(encoding="utf-8").splitlines()[0] == LEAKS_HEADER
    meta = json.loads((out / "run_metadata.json").read_text(encoding="utf-8"))
    jats_meta = meta["papers"][0]
    assert jats_meta["redaction_counts"] and jats_meta["dropped_sections"]


@needs_fixtures
def test_real_pipeline_mock_review(tmp_path, monkeypatch):
    from bars import jats

    monkeypatch.setenv("BARS_MOCK", "1")
    code, out = run_cli(tmp_path, [f"jats\t{FIXTURE_JATS}\tsynthetic_jats.xml"], "--confirm-not-confidential",
                        mode="review")
    assert code == 0
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "ok" and row["total"] == "7.2" and row["model_version"] == "mock"

    title = " ".join(jats.parse_jats(FIXTURE_JATS.read_bytes(), "synthetic_jats.xml").title.split())
    assert title
    text = (out / "reviews" / "synthetic_jats.md").read_text(encoding="utf-8")
    body, footer = text.split("**BARS provenance**", 1)
    assert f"Title (restored after review): {title}" in footer
    assert title not in body


@needs_fixtures
def test_python_dash_m_entry_point(tmp_path):
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("\n".join(fixture_manifest(tmp_path)) + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("BARS_MOCK", "BARS_GEMINI_API_KEY")}
    env["PYTHONPATH"] = str(TOOL_DIR)
    proc = subprocess.run(
        [sys.executable, "-m", "bars", "run", "--manifest", str(manifest), "--outdir", str(tmp_path / "out"),
         "--mode", "scrub", "--confirm-not-confidential"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / "scores.tsv").exists()
    assert "Quokkason" not in proc.stdout + proc.stderr


# --- regressions: identifiers, real fetch statuses, abstract-only, accounting, reruns -----------


@pytest.mark.parametrize("value, ids", [
    ("PMC 11178272", ["PMC11178272"]),
    ("PMID 15904491", ["PMID:15904491"]),
    ("PMCID PMC123", ["PMCID:PMC123"]),
    ("doi 10.1/x", ["doi:10.1/x"]),
    ("PMID: 36322581; PMCID: PMC9629619", ["PMID:36322581", "PMCID:PMC9629619"]),
    ("PMC9629619; PMC10627290", ["PMC9629619", "PMC10627290"]),
    ("PMC123 # my paper", ["PMC123"]),
    ("﻿PMC9629619", ["PMC9629619"]),
    ("(PMC1), [PMC2]", ["PMC1", "PMC2"]),
    ("10.1002/(SICI)1097-4636(199706)35:4<425::AID-JBM3>3.0.CO;2-G",
     ["10.1002/(SICI)1097-4636(199706)35:4<425::AID-JBM3>3.0.CO;2-G"]),
])
def test_split_ids_keeps_prefixed_forms_whole(value, ids):
    from bars import fetch
    assert cli.split_ids(value) == ids
    assert all(fetch.classify_id(i) for i in ids)


def test_manifest_with_byte_order_mark(tmp_path):
    manifest = tmp_path / "m.tsv"
    manifest.write_text("﻿id\tPMC1\t\n", encoding="utf-8")
    assert [(i.kind, i.value) for i in cli.read_manifest(manifest)] == [("id", "PMC1")]


class HttpResponse:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self.content, self.headers = status, payload, content, {}

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


@pytest.fixture
def real_fetch(fakes, monkeypatch):
    """The real bars.fetch behind a scripted requests.get (search hits by PMCID; fullTextXML answers)."""
    from bars import fetch

    state = SimpleNamespace(hits={}, fulltext={}, calls=[])

    def get(url, params=None, headers=None, timeout=None):
        state.calls.append(url)
        if url == fetch.SEARCH_URL:
            pmcid = params["query"].split(":", 1)[1]
            hits = state.hits.get(pmcid)
            return HttpResponse(500) if hits == "down" else HttpResponse(payload={"resultList": {"result": hits}})
        pmcid = url.rsplit("/", 2)[-2]
        return state.fulltext[pmcid]

    monkeypatch.setattr(cli, "fetch", fetch)
    monkeypatch.setattr(fetch.requests, "get", get)
    monkeypatch.setattr(fetch, "_sleep", lambda seconds: None)
    return state


def epmc_hit(pmcid, oa="Y", in_epmc="Y"):
    return [{"pmcid": pmcid, "pmid": "1", "pubYear": "2019", "isOpenAccess": oa, "inEPMC": in_epmc, "title": "T"}]


def test_real_fetch_statuses(tmp_path, real_fetch):
    missing = HttpResponse(500, payload={"status": 500, "path": "/europepmc/webservices/rest/PMC3/fullTextXML"})
    real_fetch.hits = {"PMC1": epmc_hit("PMC1"), "PMC2": epmc_hit("PMC2"), "PMC3": epmc_hit("PMC3"),
                       "PMC4": epmc_hit("PMC4", oa="N"), "PMC5": "down"}
    real_fetch.fulltext = {"PMC1": HttpResponse(content=b"<article/>"), "PMC2": HttpResponse(404), "PMC3": missing}
    code, out = run_cli(tmp_path, ["id\tPMC1 PMC2 PMC3 PMC4 PMC5"])
    assert code == 0
    rows = {r["name"]: (r["status"], r["message"]) for r in read_tsv(out / "scores.tsv")}
    assert rows["PMC1"][0] == "scrubbed"
    assert rows["PMC2"][0] == "not_open_access" and "404" in rows["PMC2"][1]
    assert rows["PMC3"][0] == "not_open_access" and "no full text" in rows["PMC3"][1]
    assert rows["PMC4"][0] == "not_open_access"  # in Europe PMC but not open access
    assert rows["PMC5"][0] == "fetch_error" and "after 4 attempts" in rows["PMC5"][1]
    assert not any("PMC4/fullTextXML" in url for url in real_fetch.calls)  # no full-text request


def test_abstract_only_record_is_not_reviewed(tmp_path, fakes, monkeypatch):
    def parse_jats(data, source_id):
        doc = fake_doc(source_id)
        doc.sections = [doc.sections[0], Section("Selected References", "These references are in PubMed.", "refs")]
        return doc

    monkeypatch.setattr(cli, "jats", SimpleNamespace(parse_jats=parse_jats))
    monkeypatch.setenv("BARS_MOCK", "1")
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "ingest_error" and "no full-text body" in row["message"]
    assert list((out / "reviews").iterdir()) == []


def test_billed_review_failure_keeps_its_accounting(tmp_path, fakes, monkeypatch):
    from google.genai import types

    response = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text="cut")]),
                                    finish_reason="MAX_TOKENS")],
        model_version="gemini-x-001",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1000, candidates_token_count=500, thoughts_token_count=32000,
            total_token_count=33500))
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: response))
    monkeypatch.setenv("BARS_GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(review, "_make_client", lambda key: client)
    code, out = run_cli(tmp_path, ["id\tPMC1"], "--model", "gemini-x", mode="review")
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "review_error" and "MAX_TOKENS" in row["message"]
    assert (row["model"], row["model_version"]) == ("gemini-x", "gemini-x-001")
    assert (row["prompt_tokens"], row["output_tokens"], row["thinking_tokens"]) == ("1000", "500", "32000")
    paper = json.loads((out / "run_metadata.json").read_text())["papers"][0]
    assert paper["finish_reason"] == "MAX_TOKENS" and paper["attempts"] == 1
    assert paper["usage"]["total_token_count"] == 33500


def test_rerun_into_the_same_outdir_leaves_no_stale_files(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("BARS_MOCK", "1")
    code, out = run_cli(tmp_path, ["id\tPMC1 PMC2"], mode="review")
    assert code == 0 and len(list((out / "reviews").iterdir())) == 2
    code, out = run_cli(tmp_path, ["id\tPMC404"], mode="review")
    assert code == 1
    assert list((out / "reviews").iterdir()) == [] and list((out / "scrubbed").iterdir()) == []


def test_key_is_redacted_before_long_errors_are_cut(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("BARS_GEMINI_API_KEY", FAKE_KEY)

    pad = 300 - len("RuntimeError: ") - len(" key=") - len(FAKE_KEY) // 2

    def leaky(*args, **kwargs):  # the 300-character cut of short_error falls in the middle of the key
        raise RuntimeError("x" * pad + f" key={FAKE_KEY} rejected")

    monkeypatch.setattr(cli.review, "review", leaky)
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 1
    written = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    for n in range(8, len(FAKE_KEY) + 1):
        assert FAKE_KEY[:n] not in written, n


def test_unexpected_errors_fail_closed(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("BARS_MOCK", "1")

    def broken(markdown):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(cli.parse, "parse_scores", broken)
    code, out = run_cli(tmp_path, ["id\tPMC1"], mode="review")
    assert code == 1
    (row,) = read_tsv(out / "scores.tsv")
    assert row["status"] == "review_error" and "internal error: RuntimeError: parser bug" in row["message"]

    paper = cli.Paper(cli.InputItem("id", "PMC1"), "PMC1", "pmcid", "PMC1", "MANUSCRIPT_001")
    assert cli.fallback_status(paper) == "ingest_error"
    paper.scrubbed = ScrubResult(text=SCRUBBED_TEXT)
    assert cli.fallback_status(paper) == "leak_blocked"
    paper.result = ReviewResult(markdown="", model="m")
    assert cli.fallback_status(paper) == "review_error"

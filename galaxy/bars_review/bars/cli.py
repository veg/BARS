"""Command-line entry point: `python -m bars run --manifest M --outdir O [...]`.

Each manifest row is one paper (an identifier fetched from Europe PMC, or an uploaded
JATS/PDF file). Papers are processed sequentially and independently:
ingest -> scrub -> leak check -> (review mode, not blocked) review -> parse scores.
A failing paper gets an error status; it never aborts the batch.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__, fetch, jats, leakcheck, parse, pdf, review, scrub
from .model import DROP_KINDS, STATUSES, Document, Leak, ReviewResult, Scores, ScrubResult

DEFAULT_MODEL = "gemini-4-argon"
DEFAULT_MAX_PAPERS = 25
DEFAULT_MAX_OUTPUT_TOKENS = 32768
MANIFEST_KINDS = ("id", "jats", "pdf")
SUCCESS_STATUSES = frozenset({"ok", "scores_invalid", "scrubbed"})
UPLOAD_SUFFIXES = (".pdf", ".xml", ".nxml", ".jats")
MIN_BODY_CHARS = 1500  # text outside the abstract below which a record is abstract-only (scanned back issues)

SCORE_COLUMNS = (
    "id", "name", "input_type", "status", "total", "core", "rigor", "claims", "biology",
    "reproducibility", "tier", "score_source", "score_issues", "leaks_block", "leaks_warn", "model",
    "model_version", "prompt_tokens", "output_tokens", "thinking_tokens", "seconds", "message",
)
LEAK_COLUMNS = ("id", "name", "kind", "severity", "match", "context")


class FatalError(Exception):
    """Invalid run configuration; reported on one stderr line with exit code 2."""


class StageError(Exception):
    """A per-paper failure carrying the model.STATUSES value to record."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class InputItem:
    kind: str  # "id", "jats" or "pdf"
    value: str  # user identifier, or path of the uploaded file
    label: str = ""  # Galaxy element identifier of an upload


@dataclass
class Paper:
    item: InputItem
    id: str  # normalised identifier (PMC123, PMID:123, doi:10.x/y) or upload label
    input_type: str  # pmcid, pmid, doi, unknown, jats or pdf
    name: str  # unique filesystem-safe output designation
    anon_id: str  # MANUSCRIPT_### shown to the reviewer
    status: str = ""
    message: str = ""
    doc: Document | None = None
    scrubbed: ScrubResult | None = None
    leaks: list[Leak] = field(default_factory=list)
    result: ReviewResult | None = None
    scores: Scores | None = None
    seconds: float = 0.0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors (2) and --help (0)
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return run(args)
    except FatalError as exc:
        print(f"bars: error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bars", description="BARS blinded AI review of manuscripts.")
    parser.add_argument("--version", action="version", version=f"bars {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("run", help="process a manifest of papers")
    cmd.add_argument("--manifest", required=True, type=Path, help="TSV: kind<TAB>value<TAB>label")
    cmd.add_argument("--outdir", required=True, type=Path)
    cmd.add_argument("--mode", choices=("review", "scrub"), default="review")
    cmd.add_argument("--model", default=DEFAULT_MODEL)
    cmd.add_argument("--max-papers", type=int, default=DEFAULT_MAX_PAPERS)
    cmd.add_argument("--redact-terms", type=Path, help="extra terms to redact, one per line or comma-separated")
    cmd.add_argument("--confirm-not-confidential", action="store_true",
                     help="uploads are the user's own or published manuscripts")
    cmd.add_argument("--temperature", type=float, default=1.0)
    cmd.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    cmd.add_argument("--seed", type=int, default=None)
    return parser


def run(args: argparse.Namespace) -> int:
    started = utc_now()
    items = read_manifest(args.manifest)
    check_run(items, args)
    terms = read_terms(args.redact_terms) if args.redact_terms else []
    papers = plan_papers(items)

    outdir: Path = args.outdir
    for sub in ("scrubbed", "reviews"):
        (outdir / sub).mkdir(parents=True, exist_ok=True)
        for stale in (outdir / sub).glob("*.md"):
            stale.unlink()  # per-paper files of an earlier run would contradict the new tables
    sha = review.template_sha256()

    for index, paper in enumerate(papers, 1):
        print(f"[{index}/{len(papers)}] {paper.id}: processing", flush=True)
        process(paper, args, terms, outdir, sha)
        detail = f" ({paper.message})" if paper.message else ""
        print(f"[{index}/{len(papers)}] {paper.id}: {paper.status}{detail}", flush=True)
        write_tables(papers[:index], outdir)

    write_metadata(papers, args, terms, sha, started, outdir)
    counts = status_counts(papers)
    summary = ", ".join(f"{n} {status}" for status, n in counts.items() if n)
    print(f"done: {summary}", flush=True)
    if any(p.status in SUCCESS_STATUSES for p in papers):
        return 0
    print(f"bars: no paper was processed successfully ({summary})", file=sys.stderr)
    return 1


# --- inputs -----------------------------------------------------------------


def read_manifest(path: Path) -> list[InputItem]:
    """Parse the manifest TSV; `id` values may hold several comma/space separated identifiers."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise FatalError(f"cannot read manifest {path}: {exc}") from None
    items = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        kind = cols[0].strip().lower()
        value = cols[1].strip() if len(cols) > 1 else ""
        label = cols[2].strip() if len(cols) > 2 else ""
        if kind not in MANIFEST_KINDS:
            expected = ", ".join(MANIFEST_KINDS)
            raise FatalError(f"manifest line {lineno}: unknown kind {kind!r} (expected one of {expected})")
        if kind == "id":
            items.extend(InputItem("id", token) for token in split_ids(value))
        elif value:
            items.append(InputItem(kind, value, label))
    return items


def split_ids(value: str) -> list[str]:
    """Identifiers in one manifest value, separated by commas, semicolons or whitespace.

    Prefixed forms stay whole ('PMC 123', 'PMID 123', 'PMID: 123', 'PMCID PMC123', 'doi 10.x/y'),
    an inline '# comment' and byte-order marks are dropped, and brackets around an id are removed.
    """
    value = value.replace("\ufeff", "")
    value = re.sub(r"(?:^|\s)#.*$", "", value)
    value = re.sub(r"(?i)\bpmc\s+(?=\d)", "PMC", value)
    value = re.sub(r"(?i)\b(pmid|pmcid|doi)\s*:?\s+(?=\S)", r"\1:", value)
    tokens = (token.strip("()[]") for token in re.split(r"[,\s]+|;(?=\s|$)", value))  # 'CO;2-G' DOIs stay
    return [token for token in tokens if token]


def read_terms(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FatalError(f"cannot read redact-terms file {path}: {exc}") from None
    terms: list[str] = []
    for raw in re.split(r"[,\n]", text):
        term = " ".join(raw.split())
        if len(term) >= 2 and term not in terms:
            terms.append(term)
    return terms


def check_run(items: list[InputItem], args: argparse.Namespace) -> None:
    """Fail fast (before any network access) on an invalid run."""
    if not items:
        raise FatalError("no inputs: the manifest lists no identifiers or files")
    if args.max_papers < 1:
        raise FatalError("--max-papers must be at least 1")
    if len(items) > args.max_papers:
        raise FatalError(f"too many inputs: {len(items)} papers given, at most {args.max_papers} per run")
    if any(item.kind != "id" for item in items) and not args.confirm_not_confidential:
        raise FatalError("uploaded manuscripts require --confirm-not-confidential "
                         "(they must not be manuscripts under confidential peer review)")
    if args.mode == "review" and not review.is_mock() and not review.resolve_key()[0]:
        raise FatalError("mode 'review' needs a Gemini API key: add your own key to your Galaxy credentials "
                         f"({review.USER_KEY_ENV}), ask the server administrator to set {review.KEY_ENV}, "
                         "or use mode 'scrub'")


def plan_papers(items: list[InputItem]) -> list[Paper]:
    taken: set[str] = set()
    papers = []
    for index, item in enumerate(items, 1):
        paper_id, input_type = identify(item)
        stem = paper_id
        if item.kind != "id" and stem.lower().endswith(UPLOAD_SUFFIXES):
            stem = stem.rsplit(".", 1)[0]
        papers.append(Paper(item, paper_id, input_type, unique_name(stem, taken), f"MANUSCRIPT_{index:03d}"))
    return papers


def identify(item: InputItem) -> tuple[str, str]:
    """(display id, input_type) for an input; unparseable identifiers keep their raw text."""
    if item.kind != "id":
        return item.label or Path(item.value).name, item.kind
    try:
        kind, value = fetch.classify_id(item.value)
    except ValueError:
        return item.value, "unknown"
    prefix = {"pmid": "PMID:", "doi": "doi:"}.get(kind, "")
    return prefix + value, kind


def unique_name(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")[:100] or "paper"
    name, n = base, 2
    while name.lower() in taken:  # case-insensitive so names are safe on any filesystem
        name, n = f"{base}_{n}", n + 1
    taken.add(name.lower())
    return name


# --- per-paper pipeline -----------------------------------------------------


def process(paper: Paper, args: argparse.Namespace, terms: list[str], outdir: Path, sha: str) -> None:
    start = time.monotonic()
    try:
        run_stages(paper, args, terms, outdir, sha)
    except StageError as exc:
        paper.status, paper.message = exc.status, exc.message
    except Exception as exc:  # noqa: BLE001 - an unexpected bug must not abort the batch
        paper.status = fallback_status(paper)
        paper.message = f"internal error: {short_error(exc)}"
    paper.message = redact_key(paper.message)
    paper.seconds = round(time.monotonic() - start, 2)


def run_stages(paper: Paper, args: argparse.Namespace, terms: list[str], outdir: Path, sha: str) -> None:
    paper.doc = ingest(paper)
    if not paper.doc.sections:
        raise StageError("ingest_error", "no text sections could be extracted")
    body_chars = sum(len(s.text) for s in paper.doc.sections if s.kind not in DROP_KINDS and s.kind != "abstract")
    if body_chars < MIN_BODY_CHARS:
        raise StageError("ingest_error", f"no full-text body: {body_chars} characters outside the abstract "
                                         f"(at least {MIN_BODY_CHARS} needed; abstract-only record?)")

    try:
        paper.scrubbed = scrub.scrub(paper.doc, terms)
    except Exception as exc:  # noqa: BLE001
        raise StageError("ingest_error", f"scrubbing failed: {short_error(exc)}") from None
    (outdir / "scrubbed" / f"{paper.name}.md").write_text(paper.scrubbed.text, encoding="utf-8")

    try:
        paper.leaks = leakcheck.check(paper.scrubbed.text, paper.doc, terms)
        blocked = leakcheck.blocking(paper.leaks)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise StageError("leak_blocked", f"leak check failed: {short_error(exc)}") from None
    if blocked:
        kinds = sorted({leak.kind for leak in paper.leaks if leak.severity == "block"})
        n_block = sum(leak.severity == "block" for leak in paper.leaks)
        raise StageError("leak_blocked", f"{n_block} blocking leak(s): {', '.join(kinds)}; not sent for review")

    if args.mode == "scrub":
        paper.status = "scrubbed"
        return

    try:
        paper.result = review.review(paper.scrubbed.text, paper.anon_id, args.model,
                                     temperature=args.temperature, max_output_tokens=args.max_output_tokens,
                                     seed=args.seed)
    except review.ReviewError as exc:
        # A completed but unusable call (MAX_TOKENS, SAFETY) was still billed: keep its accounting.
        paper.result = ReviewResult(markdown="", model=args.model, model_version=exc.model_version,
                                    finish_reason=exc.finish_reason, usage=exc.usage, attempts=exc.attempts)
        raise StageError("review_error", str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise StageError("review_error", f"review failed: {short_error(exc)}") from None
    paper.scores = parse.parse_scores(paper.result.markdown)
    # The review text is final; only now is the bibliographic identity re-attached.
    text = paper.result.markdown.rstrip("\n") + "\n" + provenance_footer(paper, sha)
    (outdir / "reviews" / f"{paper.name}.md").write_text(text, encoding="utf-8")
    paper.status = "scores_invalid" if paper.scores.issues else "ok"
    if paper.scores.issues:
        paper.message = f"{len(paper.scores.issues)} score issue(s)"


def ingest(paper: Paper) -> Document:
    item = paper.item
    if item.kind == "id":
        return ingest_id(item.value, paper.id)
    try:
        data = Path(item.value).read_bytes()
    except OSError as exc:
        raise StageError("ingest_error", f"cannot read uploaded file: {exc.strerror or exc}") from None
    parser = pdf.parse_pdf if item.kind == "pdf" else jats.parse_jats
    try:
        return parser(data, paper.id)
    except Exception as exc:  # noqa: BLE001
        raise StageError("ingest_error", f"cannot parse {item.kind.upper()}: {short_error(exc)}") from None


def ingest_id(raw: str, source_id: str) -> Document:
    """Resolve an identifier in Europe PMC and parse its open-access JATS."""
    try:
        kind, value = fetch.classify_id(raw)
    except ValueError:
        raise StageError("fetch_error", f"not a PMCID, PMID or DOI: {raw!r}") from None
    try:
        meta = fetch.resolve(kind, value)
        pmcid = meta.get("pmcid") or ""
        xml = fetch.fetch_jats(pmcid) if pmcid and meta.get("is_open_access") else None
    except fetch.NotOpenAccess as exc:
        raise StageError("not_open_access", str(exc) or "no open-access full text") from None
    except fetch.FetchError as exc:
        raise StageError("fetch_error", str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise StageError("fetch_error", short_error(exc)) from None
    if xml is None:
        raise StageError("not_open_access", "no open-access full text in Europe PMC")
    try:
        doc = jats.parse_jats(xml, source_id)
    except Exception as exc:  # noqa: BLE001
        raise StageError("ingest_error", f"cannot parse JATS: {short_error(exc)}") from None
    fill_from_metadata(doc, meta)
    return doc


def fill_from_metadata(doc: Document, meta: dict[str, Any]) -> None:
    """Use Europe PMC metadata where the JATS lacked it (feeds scrubbing and leak checks)."""
    for attr in ("title", "doi", "pmid", "pmcid"):
        if not getattr(doc, attr) and meta.get(attr):
            setattr(doc, attr, str(meta[attr]))
    if doc.year is None and str(meta.get("year") or "").isdigit():
        doc.year = int(meta["year"])


def fallback_status(paper: Paper) -> str:
    """Status for an unexpected error, from how far the paper got (fail closed before review)."""
    if paper.scrubbed is None:
        return "ingest_error"
    if paper.result is None:
        return "leak_blocked"
    return "review_error"


def redact_key(text: str) -> str:
    """Defence in depth: no API key (user's or server's) may reach outputs or logs."""
    for key in review.env_keys():
        text = text.replace(key, "[API_KEY]")
    return text


def short_error(exc: BaseException) -> str:
    """One-line error text, key-redacted before it is cut (a cut key would no longer be found)."""
    text = redact_key(" ".join(f"{type(exc).__name__}: {exc}".split()))
    return text[:300]


def provenance_footer(paper: Paper, sha: str) -> str:
    result = paper.result
    assert result is not None and paper.doc is not None
    title = " ".join(paper.doc.title.split()) or "unknown"
    lines = [
        "",
        "---",
        "",
        "**BARS provenance**",
        "",
        f"- Source: {paper.id}",
        f"- Title (restored after review): {title}",
        f"- Reviewed as: {paper.anon_id}",
        f"- Model requested: {result.model}",
        f"- Model version: {result.model_version or 'unknown'}",
        f"- Review template SHA-256: {sha}",
        f"- BARS version: {__version__}",
        f"- Generated (UTC): {utc_now()}",
        f"- Mock review: {'yes' if result.mock else 'no'}",
        "",
    ]
    return "\n".join(lines)


# --- outputs ----------------------------------------------------------------


def write_tables(papers: list[Paper], outdir: Path) -> None:
    write_tsv(outdir / "scores.tsv", SCORE_COLUMNS, (score_row(p) for p in papers))
    leak_rows = ([p.id, p.name, leak.kind, leak.severity, leak.match, leak.context]
                 for p in papers for leak in p.leaks)
    write_tsv(outdir / "leaks.tsv", LEAK_COLUMNS, leak_rows)


def score_row(paper: Paper) -> list[Any]:
    scores = paper.scores or Scores()
    result = paper.result
    usage = result.usage if result else {}
    return [
        paper.id, paper.name, paper.input_type, paper.status,
        num(scores.total), num(scores.core), num(scores.rigor), num(scores.claims),
        num(scores.biology), num(scores.reproducibility), scores.tier,
        scores.source if paper.scores else "", "; ".join(scores.issues),
        sum(leak.severity == "block" for leak in paper.leaks),
        sum(leak.severity == "warn" for leak in paper.leaks),
        result.model if result else "", result.model_version if result else "",
        usage.get("prompt_token_count", ""), usage.get("candidates_token_count", ""),
        usage.get("thoughts_token_count", ""), paper.seconds, paper.message,
    ]


def num(value: float | None) -> str:
    return "" if value is None else str(round(value, 3))


def write_tsv(path: Path, header: Iterable[str], rows: Iterable[list[Any]]) -> None:
    lines = ["\t".join(header)]
    lines += ["\t".join(tsv_cell(cell) for cell in row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tsv_cell(value: Any) -> str:
    return " ".join(str(value).replace("\t", " ").split()) if value is not None else ""


def status_counts(papers: list[Paper]) -> dict[str, int]:
    return {status: sum(p.status == status for p in papers) for status in STATUSES}


def write_metadata(papers: list[Paper], args: argparse.Namespace, terms: list[str], sha: str,
                   started: str, outdir: Path) -> None:
    data = {
        "package": "bars",
        "package_version": __version__,
        "google_genai_version": package_version("google-genai"),
        "python_version": platform.python_version(),
        "mode": args.mode,
        "model": args.model,
        "temperature": args.temperature,
        "max_output_tokens": args.max_output_tokens,
        "seed": args.seed,
        "tools": [],
        "template_sha256": sha,
        "mock": review.is_mock(),
        "api_key_source": "mock" if review.is_mock() else (review.resolve_key()[1] or "none"),
        "max_papers": args.max_papers,
        "n_redact_terms": len(terms),
        "started_utc": started,
        "finished_utc": utc_now(),
        "n_inputs": len(papers),
        "status_counts": status_counts(papers),
        "papers": [paper_metadata(p) for p in papers],
    }
    (outdir / "run_metadata.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def paper_metadata(paper: Paper) -> dict[str, Any]:
    result = paper.result
    return {
        "id": paper.id,
        "name": paper.name,
        "input_type": paper.input_type,
        "anon_id": paper.anon_id,
        "status": paper.status,
        "message": paper.message,
        "model_version": result.model_version if result else "",
        "finish_reason": result.finish_reason if result else "",
        "attempts": result.attempts if result else 0,
        "review_seconds": result.seconds if result else 0.0,
        "usage": result.usage if result else {},
        "redaction_counts": paper.scrubbed.counts if paper.scrubbed else {},
        "dropped_sections": paper.scrubbed.dropped_sections if paper.scrubbed else [],
        "notes": paper.doc.notes if paper.doc else [],
        "seconds": paper.seconds,
    }


def package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

#!/usr/bin/env python3
"""
Optional post-audit lookup: does a reviewed paper cite work that has a retraction notice?

BARS strips references and forbids retraction lookups during the audit, so the
audit can't see this. This script runs after it and apart from it. It reads only
the paper's DOI, PMID or PMCID from metadata.json and asks the free SaneCite API
which of the paper's references carry a notice (retraction, expression of concern
or reinstatement). Notice data comes from the Retraction Watch database via Crossref.

It never changes metadata.json, review.md, or any score. With --write it adds one
file, cited_retractions.json, beside metadata.json. Without --write it only prints.
Don't show its output to a reviewer before the audit, since that would break blinding.

A listed reference is not a finding against the paper: a citing paper may have been
published before the notice (see `timing`) or may cite the work to criticize it. A
preprint can be listed when its journal version was retracted, and `reasons` can show
a retract-and-replace, so read the notice itself before relying on an entry.

Usage:
  python3 scripts/check_cited_retractions.py reviews/2013/pmc4225573-gao-gene-expression-profiling-analysis-of
  python3 scripts/check_cited_retractions.py --all --write
"""

import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEWS_DIR = ROOT / "reviews"
API = "https://sanecite.com/api/check"
OUTPUT_NAME = "cited_retractions.json"
DELAY_SECONDS = 3.1  # The API allows 20 calls a minute.
TIMEOUT_SECONDS = 30
MAX_RATE_LIMIT_WAIT = 90


def identifiers(meta: dict) -> list[str]:
    """The paper's IDs, best first. `id` is a PMCID only when it says so."""
    ids = [str(meta.get("doi") or "").strip(), str(meta.get("pmid") or "").strip()]
    record_id = str(meta.get("id") or "").strip()
    if record_id.upper().startswith("PMC"):
        ids.append(record_id)
    return [value for value in ids if value]


def fetch(identifier: str, api: str, sleep=time.sleep) -> dict | None:
    """The API's answer for one ID, or None if it doesn't know the ID."""
    url = f"{api}?{urllib.parse.urlencode({'q': identifier})}"
    request = urllib.request.Request(url, headers={"User-Agent": "BARS-check-cited-retractions"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                answer = json.load(response)
            return answer if answer.get("ok") else None
        except urllib.error.HTTPError as err:
            if err.code == 429:
                if attempt == 1:
                    break
                try:
                    wait = int(err.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 60
                sleep(min(max(wait, 1), MAX_RATE_LIMIT_WAIT))
                continue
            if 400 <= err.code < 500:
                return None
            raise
    raise RuntimeError("still rate limited after waiting")


def check(meta: dict, api: str = API, sleep=time.sleep) -> dict | None:
    """The record for one paper, or None if the API knows none of its IDs."""
    for identifier in identifiers(meta):
        answer = fetch(identifier, api, sleep)
        if answer is None:
            continue
        paper = answer.get("paper") or {}
        return {
            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "queried": identifier,
            "api": api,
            "data": answer.get("data"),
            "refs_total": answer.get("refs_total"),
            "refs_checked": answer.get("refs_checked"),
            "paper_own_notice": paper.get("own"),
            "flagged": answer.get("flagged") or [],
        }
    return None


def metadata_paths(targets: list[str], check_all: bool) -> list[Path]:
    if check_all:
        return sorted(REVIEWS_DIR.glob("**/metadata.json"))
    return [path / "metadata.json" if path.is_dir() else path for path in map(Path, targets)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="*", help="Review folders or metadata.json files")
    parser.add_argument("--all", action="store_true", help="Check every review under reviews/")
    parser.add_argument("--write", action="store_true", help=f"Write {OUTPUT_NAME} beside each metadata.json")
    parser.add_argument("--api", default=API, help=argparse.SUPPRESS)
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if bool(args.targets) == args.all:
        parser.error("give review folders, or --all, but not both")

    counts = {"checked": 0, "with_flags": 0, "unknown": 0, "errors": 0}
    paths = metadata_paths(args.targets, args.all)
    for index, path in enumerate(paths):
        if index:
            time.sleep(args.delay)
        try:
            record = check(json.loads(path.read_text(encoding="utf-8")), args.api)
        except (OSError, ValueError, RuntimeError) as err:
            counts["errors"] += 1
            sys.stderr.write(f"{path.parent}: {err}\n")
            continue
        if record is None:
            counts["unknown"] += 1
            print(f"{path.parent}: not found")
            continue
        counts["checked"] += 1
        counts["with_flags"] += bool(record["flagged"])
        print(f"{path.parent}: {len(record['flagged'])} flagged of {record['refs_checked']} references checked")
        if args.write:
            output = path.parent / OUTPUT_NAME
            output.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            for reference in record["flagged"]:
                print(f"  {reference.get('status')}: {reference.get('doi') or reference.get('pmid')}")

    print(json.dumps(counts))
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

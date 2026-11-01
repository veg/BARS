# BARS Galaxy tool: developer notes

`bars_review` wraps BARS (Blinded AI Review System): fetch or ingest a manuscript, de-identify it, leak-check it
(fail closed), review it with Gemini against [`REVIEW_TEMPLATE.md`](../REVIEW_TEMPLATE.md) and parse the scores.
User-facing documentation is the `<help>` section of `bars_review/bars_review.xml`.

## Layout

```
galaxy/bars_review/
  bars_review.xml   Galaxy tool wrapper
  macros.xml        tokens (version, model, paper cap), requirements, sanitizer, citation
  .shed.yml         Tool Shed metadata (owner nekrut, repository bars_review)
  bars/             python package shipped in the tool directory, run as `python -m bars`
  test-data/        synthetic fixtures only (no real papers, no recorded API output)
tests/bars/         pytest suite (no network, no API key)
.github/workflows/bars.yml   CI: pytest, planemo lint + test
```

- `bars/REVIEW_TEMPLATE.md` must stay identical to the repo-root `REVIEW_TEMPLATE.md` (a pytest checks this); copy it
  after editing the root template.
- `@TOOL_VERSION@` in `macros.xml` follows `bars.__version__`; bump `@VERSION_SUFFIX@` for wrapper-only changes.
- Requirement versions in `macros.xml` and the `pip install` pins in the CI workflow must match.
- `tests/bars/test_tool_consistency.py` checks these, plus `@MODEL@`/`@MAX_PAPERS@` against `bars.cli` and the
  template's tier table against `bars.parse.TIERS`; `bars/` must hold only `*.py` and `REVIEW_TEMPLATE.md`.

## Running the checks

```bash
pip install google-genai==2.34.0 pymupdf==2.0 lxml==6.1.3 requests==2.34.2 pytest==9.2.0 planemo==0.75.47
python -m pytest tests/bars
planemo lint --fail_level warn galaxy/bars_review
planemo shed_lint --tools --fail_level warn galaxy/bars_review
planemo test --conda_dependency_resolution galaxy/bars_review
```

The planemo tests use scrub mode on the synthetic uploads plus three argument checks (exit code 2, asserted on the
stderr message) that fail before any network access, so they need neither a key nor network beyond conda.

## Running the CLI directly

```bash
printf 'jats\tgalaxy/bars_review/test-data/synthetic_jats.xml\tsynthetic_jats.xml\n' > manifest.tsv
PYTHONPATH=galaxy/bars_review python -m bars run --manifest manifest.tsv --outdir out \
    --mode scrub --confirm-not-confidential
```

Manifest: TSV without header, `kind<TAB>value<TAB>label`, kind `id` (value = PMCID/PMID/DOI, several may be
comma/space separated; label empty), `jats` or `pdf` (value = file path, label = Galaxy element identifier). Outputs in
`out/`: `scores.tsv`, `leaks.tsv`, `run_metadata.json`, `scrubbed/<name>.md`, `reviews/<name>.md`. Exit code 0 if at
least one paper succeeded, 1 if none did, 2 for an invalid run (no or too many inputs, uploads without
`--confirm-not-confidential`, review mode without a key). A rerun into the same `--outdir` replaces the tables and
deletes the `*.md` files a previous run left in `scrubbed/` and `reviews/`.

- **Scrub mode** (`--mode scrub`): ingest, de-identify and leak-check only; no API call and no key needed (identifiers
  are still fetched from Europe PMC). Use it to inspect exactly what the reviewer would see.
- **`BARS_MOCK=1`**: review mode returns a canned, valid review (model version `mock`) without network or key, to
  exercise the review, parse and provenance path end to end. Never set it on a production server.
- **API key**: two sources, in order of precedence. The run metadata records which one was used (`api_key_source`:
  `user`, `server`, `mock` or `none`), never the key. Never commit a key or write it to test data.
  1. `BARS_USER_GEMINI_API_KEY`: the user's own key. The tool declares an optional `<credentials>` secret
     (`bars_gemini`), so on Galaxy 25.1+ with a vault configured each user can store a key in their preferences and
     Galaxy injects it into their jobs only (and passes it into containers).
  2. `BARS_GEMINI_API_KEY`: the server's key. Admins set it in the job environment of the destination that runs
     `bars_review` (visible to them in job scripts; containerised destinations must pass it through). The variable in
     the Galaxy server's own environment does **not** reach jobs: the local runner starts jobs with a clean
     environment (`HOME`, `LC_CTYPE`, `PATH`, `TMPDIR` only), so it has to come from the destination, e.g. in
     `job_conf.yml`:

     ```yaml
     execution:
       environments:
         bars:
           runner: local
           env:
             - file: /etc/galaxy/bars_secrets.sh   # export BARS_GEMINI_API_KEY=...; readable by the Galaxy user only
     tools:
       - id: bars_review
         environment: bars
     ```

- **Spending**: the 25-paper cap (`@MAX_PAPERS@` in `macros.xml`) applies per job, not per user. Mapping the tool
  over a collection (a list of identifier files, or a `list:list` of uploads) starts one job per element, each making
  up to 25 billed Gemini calls (up to 32,768 output tokens plus thinking each) on the shared key. Before enabling review
  mode, limit concurrent `bars_review` jobs per user (TPV or `job_conf.yml` limits), consider restricting the tool to a
  user group, and use a dedicated key in a Google Cloud project with a quota or budget alert.

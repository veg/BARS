"""Send a de-identified manuscript to Gemini with REVIEW_TEMPLATE.md as the rubric.

The model gets the scrubbed markdown and an anonymous manuscript ID only. No tools
(web search, URL context) are enabled. The API key is the user's own (BARS_USER_GEMINI_API_KEY,
injected by Galaxy from the user's credentials) or else the server's (BARS_GEMINI_API_KEY, set by
the administrator); it is never included in exceptions or results.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .model import ReviewResult

USER_KEY_ENV = "BARS_USER_GEMINI_API_KEY"  # the user's own key, from Galaxy user credentials
KEY_ENV = "BARS_GEMINI_API_KEY"  # the server's key, from the administrator's job environment
KEY_ENVS = (USER_KEY_ENV, KEY_ENV)  # in order of precedence
MOCK_ENV = "BARS_MOCK"
TEMPLATE_PATH = Path(__file__).with_name("REVIEW_TEMPLATE.md")
ANON_ID_RE = re.compile(r"MANUSCRIPT_\d+")

RETRY_CODES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
BACKOFF_BASE = 5.0  # seconds
BACKOFF_CAP = 120.0  # seconds
REQUEST_TIMEOUT_MS = 600_000  # per HTTP request; long thinking responses take minutes
OK_FINISH_REASONS = frozenset({"", "STOP"})

SYSTEM_PREAMBLE = """\
You are an anonymous, independent peer reviewer for the Blinded AI Review System (BARS).

- You have no web or tool access. Judge the manuscript only on the text provided. Do not try to \
identify, guess or mention the authors, their institutions, the journal or the publication date.
- The input is de-identified manuscript text. Figure images and supplementary files are not \
included; figure legends and tables are included as text. Do not penalise the manuscript because \
figure images or supplementary files are missing from this input.
- Bracketed tokens such as [CODE_REPO_URL], [GALAXY_HISTORY_URL], [GALAXY_WORKFLOW_URL], \
[WORKFLOW_REGISTRY_URL], [DATA_ARCHIVE_URL], [DATA_ARCHIVE_DOI] and [PUBLIC_DATABASE_URL] mark \
links that WERE present in the original manuscript and were removed for blinding. For \
Reproducibility, treat the code, workflow, history or data as available at that link. Other \
bracketed tokens ([AUTHOR_REDACTED], [INSTITUTION_REDACTED], [EMAIL_REDACTED], [URL_REDACTED], \
[DOI_REDACTED], [ID_REDACTED], [POST_T0_YEAR_REDACTED] ...) are blinding redactions, not errors \
in the manuscript.
- Fill the template's Manuscript Title, Authors and Journal / Preprint header fields with \
[REDACTED FOR BLINDED REVIEW], and use the manuscript ID given in the request as [MANUSCRIPT_ID].
- Follow REVIEW_TEMPLATE.md below exactly: run the audit and scoring of Parts 0-3 and write the \
review in the Part 4 layout (do not wrap the review itself in a code fence).
- Finally, append exactly one fenced ```json block with your final scores as numbers and one of \
the five tier names, where total = rigor + claims + biology + reproducibility and \
core = rigor + claims + biology:
  {"rigor": x, "claims": x, "biology": x, "reproducibility": x, "total": x, "core": x, "tier": "..."}
"""

MOCK_REVIEW = """\
# Review of {anon_id}

**Manuscript Title:** [REDACTED FOR BLINDED REVIEW]
**Authors:** [REDACTED FOR BLINDED REVIEW]
**Journal / Preprint:** [REDACTED FOR BLINDED REVIEW]
**Overall Score:** **7.2 / 10.0** (Strong / Sound) | **Scientific Core Score:** **6.0 / 8.0**
* **Score Breakdown:** Rigor: 2.6/3.5 | Claims: 1.8/2.5 | Biology: 1.6/2.0 | Reproducibility: 1.2/2.0

---

### 1. SUMMARY AND OVERALL IMPACT STATEMENT

This is a canned BARS_MOCK review. No model was called; the manuscript ({n_chars} characters of \
de-identified text) was not evaluated.

The scores below are fixed placeholder values used to exercise the pipeline.

---

### 2. SPECIFIC CONCLUSIONS AND ASSESSMENT OF SUPPORT

* **Mock claim: Moderately Supported.**
  Placeholder assessment.

---

### 3. CONSTRUCTIVE AND DOABLE SUGGESTIONS FOR IMPROVEMENT

#### 1. Replace this mock review with a real one
* **WHY:** BARS_MOCK=1 was set.
* **WHAT:** Run without BARS_MOCK.
* **HOW:** Unset BARS_MOCK and provide BARS_GEMINI_API_KEY.

```json
{{"rigor": 2.6, "claims": 1.8, "biology": 1.6, "reproducibility": 1.2,
 "total": 7.2, "core": 6.0, "tier": "Strong / Sound"}}
```
"""


class ReviewError(Exception):
    """The review could not be obtained (missing key, API failure, empty/blocked/truncated output).

    When the API answered but the answer was unusable, `usage` and `model_version` still record
    what was billed.
    """

    def __init__(self, message: str, finish_reason: str = "", attempts: int = 0,
                 usage: dict[str, int] | None = None, model_version: str = ""):
        super().__init__(message)
        self.finish_reason = finish_reason
        self.attempts = attempts
        self.usage = usage or {}
        self.model_version = model_version


def template_text() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def template_sha256() -> str:
    return hashlib.sha256(TEMPLATE_PATH.read_bytes()).hexdigest()


def build_system_instruction() -> str:
    return SYSTEM_PREAMBLE + "\n\n" + template_text()


def build_user_message(scrubbed_text: str, anon_id: str) -> str:
    """Wrap the scrubbed manuscript; anon_id must be an anonymous MANUSCRIPT_### designation."""
    if not ANON_ID_RE.fullmatch(anon_id):
        raise ValueError(f"anon_id must look like MANUSCRIPT_001, got {anon_id!r}")
    return (
        f"Manuscript ID: {anon_id}\n\n"
        f"Review the de-identified manuscript below following the system instructions. "
        f"Use {anon_id} as [MANUSCRIPT_ID].\n\n"
        f"<manuscript>\n{scrubbed_text.strip()}\n</manuscript>\n"
    )


def is_mock() -> bool:
    return os.environ.get(MOCK_ENV) == "1"


def resolve_key() -> tuple[str, str]:
    """Return (key, source): the user's key if set, else the server's; source is "user", "server" or ""."""
    for env, source in ((USER_KEY_ENV, "user"), (KEY_ENV, "server")):
        key = os.environ.get(env, "").strip()
        if key:
            return key, source
    return "", ""


def env_keys() -> list[str]:
    """Every API key present in the environment, for redaction."""
    return [key for key in (os.environ.get(env, "").strip() for env in KEY_ENVS) if key]


def mock_review(scrubbed_text: str, anon_id: str, model: str) -> ReviewResult:
    markdown = MOCK_REVIEW.format(anon_id=anon_id, n_chars=len(scrubbed_text))
    return ReviewResult(markdown=markdown, model=model, model_version="mock",
                        finish_reason="STOP", mock=True, attempts=0)


def review(
    scrubbed_text: str,
    anon_id: str,
    model: str,
    temperature: float = 1.0,
    max_output_tokens: int = 32768,
    seed: int | None = None,
    api_key: str | None = None,
    client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ReviewResult:
    """Review one scrubbed manuscript; raise ReviewError on any failure."""
    contents = build_user_message(scrubbed_text, anon_id)
    if is_mock():
        return mock_review(scrubbed_text, anon_id, model)

    key = (api_key or resolve_key()[0]).strip()
    if client is None:
        if not key:
            raise ReviewError(f"no Gemini API key: neither {USER_KEY_ENV} (your Galaxy credentials) nor "
                              f"{KEY_ENV} (set by the server administrator) is set")
        try:
            client = _make_client(key)
        except Exception as exc:  # noqa: BLE001
            raise ReviewError(f"cannot create Gemini client: {_describe(exc, key)}") from None
    config = _make_config(temperature, max_output_tokens, seed)

    start = time.monotonic()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            break
        except Exception as exc:  # noqa: BLE001 - every failure becomes a key-free ReviewError
            if attempt == MAX_ATTEMPTS or not is_transient(exc):
                raise ReviewError(
                    f"Gemini API call failed after {attempt} attempt(s): {_describe(exc, key)}",
                    attempts=attempt,
                ) from None
            sleep(backoff_delay(attempt, exc))

    model_version = str(getattr(response, "model_version", "") or "")
    try:
        markdown, finish_reason = _extract_text(response, max_output_tokens, attempt)
    except ReviewError as exc:
        exc.usage, exc.model_version = _usage(response), model_version
        raise
    return ReviewResult(
        markdown=markdown,
        model=model,
        model_version=model_version,
        finish_reason=finish_reason,
        usage=_usage(response),
        attempts=attempt,
        seconds=round(time.monotonic() - start, 3),
    )


def _make_client(key: str) -> Any:
    from google import genai
    from google.genai import types

    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))


def _make_config(temperature: float, max_output_tokens: int, seed: int | None) -> Any:
    from google.genai import types

    # Deliberately no `tools`: the reviewer must not browse or fetch URLs.
    return types.GenerateContentConfig(
        system_instruction=build_system_instruction(),
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        seed=seed,
    )


def is_transient(exc: BaseException) -> bool:
    """True for rate limits, 5xx server errors, connection failures and timeouts."""
    import httpx
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        return exc.code in RETRY_CODES
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                            ConnectionError, TimeoutError))


def backoff_delay(attempt: int, exc: BaseException | None = None) -> float:
    """Exponential backoff (5, 10, 20, 40 s ...) plus up to 25 % jitter, capped at 120 s.

    A server-suggested retryDelay (Gemini 429 RetryInfo) is honoured when longer.
    """
    delay = BACKOFF_BASE * 2 ** (attempt - 1)
    delay += random.uniform(0, 0.25 * delay)
    hinted = _retry_hint(exc)
    if hinted is not None:
        delay = max(delay, hinted)
    return min(BACKOFF_CAP, delay)


def _retry_hint(exc: BaseException | None) -> float | None:
    match = re.search(r"retryDelay'?\"?\s*:\s*'?\"?(\d+(?:\.\d+)?)s", str(getattr(exc, "details", "")))
    return float(match.group(1)) if match else None


def _describe(exc: BaseException, key: str) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if key:
        message = message.replace(key, "[API_KEY]")
    message = " ".join(message.split())
    return message[:500]


def _extract_text(response: Any, max_output_tokens: int, attempts: int) -> tuple[str, str]:
    """Return (markdown, finish_reason) or raise ReviewError for blocked/truncated/empty output."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        feedback = getattr(response, "prompt_feedback", None)
        block = _enum_name(getattr(feedback, "block_reason", None))
        if block:
            raise ReviewError(f"prompt blocked by the API (block_reason={block})",
                              finish_reason=f"BLOCKED_{block}", attempts=attempts)
        raise ReviewError("API returned no candidates", attempts=attempts)

    finish_reason = _enum_name(getattr(candidates[0], "finish_reason", None))
    if finish_reason == "MAX_TOKENS":
        raise ReviewError(f"review truncated at max_output_tokens={max_output_tokens} (finish_reason=MAX_TOKENS)",
                          finish_reason=finish_reason, attempts=attempts)
    if finish_reason not in OK_FINISH_REASONS:
        raise ReviewError(f"review not completed (finish_reason={finish_reason})",
                          finish_reason=finish_reason, attempts=attempts)
    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise ReviewError(f"API returned empty text (finish_reason={finish_reason or 'unknown'})",
                          finish_reason=finish_reason, attempts=attempts)
    return text + "\n", finish_reason


def _enum_name(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _usage(response: Any) -> dict[str, int]:
    meta = getattr(response, "usage_metadata", None)
    usage = {}
    for name in ("prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count"):
        value = getattr(meta, name, None)
        if isinstance(value, int):
            usage[name] = value
    return usage

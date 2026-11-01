"""Gemini review call (bars.review) with a fake client: retries, failures, key hygiene. No network."""

import hashlib
import traceback

import httpx
import pytest
from google.genai import errors, types

from bars import review
from bars.parse import parse_scores
from bars.review import ReviewError

FAKE_KEY = "AIzaFAKE-test-key-0123456789abcdef"
REVIEW_MD = "# Review of MANUSCRIPT_001\n\n**Overall Score:** **7.2 / 10.0** (Strong / Sound)\n"


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, *outcomes):
        self.models = FakeModels(outcomes)


def make_response(text=REVIEW_MD, finish="STOP", model_version="gemini-test-001", thought=None):
    parts = [types.Part(text=thought, thought=True)] if thought else []
    if text is not None:
        parts.append(types.Part(text=text))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        model_version=model_version,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1000, candidates_token_count=500, thoughts_token_count=200, total_token_count=1700),
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("BARS_MOCK", raising=False)
    monkeypatch.delenv("BARS_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("BARS_USER_GEMINI_API_KEY", raising=False)


@pytest.fixture
def sleeps():
    return []


def call(client, sleeps, **kwargs):
    return review.review("Scrubbed manuscript text.", "MANUSCRIPT_001", "gemini-test", client=client,
                         sleep=sleeps.append, **kwargs)


def test_system_instruction_is_preamble_plus_template():
    instruction = review.build_system_instruction()
    template = review.TEMPLATE_PATH.read_text(encoding="utf-8")
    assert instruction == review.SYSTEM_PREAMBLE + "\n\n" + template
    for token in ("[CODE_REPO_URL]", "[GALAXY_HISTORY_URL]", "[DATA_ARCHIVE_DOI]", "[REDACTED FOR BLINDED REVIEW]",
                  "no web or tool access", '"reproducibility"', '"core"', '"tier"'):
        assert token in review.SYSTEM_PREAMBLE
    assert review.template_sha256() == hashlib.sha256(review.TEMPLATE_PATH.read_bytes()).hexdigest()


def test_user_message_carries_anon_id_and_text_only():
    message = review.build_user_message("  Body text here.\n", "MANUSCRIPT_007")
    assert "MANUSCRIPT_007" in message
    assert "<manuscript>\nBody text here.\n</manuscript>" in message


@pytest.mark.parametrize("bad", ["PMC1234567", "doi:10.1/x", "MANUSCRIPT_", "manuscript_001", "MANUSCRIPT_001 PMC1"])
def test_user_message_rejects_real_identifiers(bad):
    with pytest.raises(ValueError):
        review.build_user_message("text", bad)


def test_success_records_version_usage_and_sends_no_tools(sleeps):
    client = FakeClient(make_response(thought="private reasoning"))
    result = call(client, sleeps, temperature=0.5, max_output_tokens=1234, seed=42)

    assert result.markdown == REVIEW_MD.strip() + "\n"
    assert "private reasoning" not in result.markdown
    assert result.model == "gemini-test"
    assert result.model_version == "gemini-test-001"
    assert result.finish_reason == "STOP"
    assert result.usage == {"prompt_token_count": 1000, "candidates_token_count": 500,
                            "thoughts_token_count": 200, "total_token_count": 1700}
    assert result.attempts == 1 and not result.mock and sleeps == []

    (kwargs,) = client.models.calls
    assert kwargs["model"] == "gemini-test"
    assert kwargs["contents"] == review.build_user_message("Scrubbed manuscript text.", "MANUSCRIPT_001")
    config = kwargs["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.tools is None and config.tool_config is None
    assert config.system_instruction == review.build_system_instruction()
    assert (config.temperature, config.max_output_tokens, config.seed) == (0.5, 1234, 42)


@pytest.mark.parametrize("transient", [
    errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}}),
    errors.ServerError(500, {"error": {"code": 500, "message": "internal"}}),
    errors.ServerError(504, {"error": {"code": 504, "status": "DEADLINE_EXCEEDED", "message": "timeout"}}),
    errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}),
    httpx.ConnectError("connection refused"),
    httpx.ReadTimeout("timed out"),
    ConnectionResetError("reset"),
])
def test_transient_errors_are_retried(sleeps, transient):
    client = FakeClient(transient, make_response())
    result = call(client, sleeps)
    assert result.attempts == 2
    assert len(sleeps) == 1 and 5.0 <= sleeps[0] <= 6.25


def test_gives_up_after_five_attempts_with_exponential_backoff(sleeps):
    client = FakeClient(*[errors.ServerError(502, {"error": {"message": "bad gateway"}})] * 5)
    with pytest.raises(ReviewError) as info:
        call(client, sleeps)
    assert info.value.attempts == 5
    assert "after 5 attempt(s)" in str(info.value)
    assert len(client.models.calls) == 5
    assert len(sleeps) == 4
    for delay, base in zip(sleeps, (5, 10, 20, 40), strict=True):
        assert base <= delay <= base * 1.25


def test_backoff_is_capped_and_honours_retry_delay_hint():
    assert review.backoff_delay(10) == review.BACKOFF_CAP
    hinted = errors.ClientError(429, {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "33s"}]}})
    assert review.backoff_delay(1, hinted) == 33.0
    huge = errors.ClientError(429, {"error": {"details": [{"retryDelay": "900s"}]}})
    assert review.backoff_delay(1, huge) == review.BACKOFF_CAP


@pytest.mark.parametrize("permanent", [
    errors.ClientError(400, {"error": {"code": 400, "message": "invalid argument"}}),
    errors.ClientError(403, {"error": {"code": 403, "message": "permission denied"}}),
    errors.ClientError(404, {"error": {"code": 404, "message": "model not found"}}),
    ValueError("bad request shape"),
])
def test_permanent_errors_are_not_retried(sleeps, permanent):
    client = FakeClient(permanent, make_response())
    with pytest.raises(ReviewError) as info:
        call(client, sleeps)
    assert info.value.attempts == 1
    assert sleeps == []
    assert len(client.models.calls) == 1


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "OTHER"])
def test_incomplete_finish_reasons_raise(sleeps, finish):
    with pytest.raises(ReviewError) as info:
        call(FakeClient(make_response(finish=finish)), sleeps)
    assert info.value.finish_reason == finish
    assert finish in str(info.value)
    assert sleeps == []


def test_billed_incomplete_answer_keeps_usage_and_version(sleeps):
    with pytest.raises(ReviewError) as info:
        call(FakeClient(make_response(finish="MAX_TOKENS")), sleeps)
    assert info.value.model_version == "gemini-test-001"
    assert info.value.usage["total_token_count"] == 1700 and info.value.attempts == 1


def test_no_candidates_raises(sleeps):
    with pytest.raises(ReviewError, match="no candidates"):
        call(FakeClient(types.GenerateContentResponse(candidates=[])), sleeps)


def test_empty_text_raises(sleeps):
    with pytest.raises(ReviewError, match="empty text") as info:
        call(FakeClient(make_response(text="   ")), sleeps)
    assert info.value.finish_reason == "STOP"


def test_only_thoughts_is_empty(sleeps):
    with pytest.raises(ReviewError, match="empty text"):
        call(FakeClient(make_response(text=None, thought="thinking only")), sleeps)


def test_blocked_prompt_raises(sleeps):
    response = types.GenerateContentResponse(
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=types.BlockedReason.SAFETY))
    with pytest.raises(ReviewError, match="prompt blocked") as info:
        call(FakeClient(response), sleeps)
    assert info.value.finish_reason == "BLOCKED_SAFETY"


def test_missing_key_raises_before_any_call():
    with pytest.raises(ReviewError, match="no Gemini API key"):
        review.review("text", "MANUSCRIPT_001", "gemini-test")


def test_key_taken_from_env_only(monkeypatch, sleeps):
    monkeypatch.setenv("GEMINI_API_KEY", "other-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "other-key")
    with pytest.raises(ReviewError, match="no Gemini API key"):
        review.review("text", "MANUSCRIPT_001", "gemini-test")

    seen = []
    monkeypatch.setattr(review, "_make_client", lambda key: seen.append(key) or FakeClient(make_response()))
    monkeypatch.setenv("BARS_GEMINI_API_KEY", f" {FAKE_KEY}\n")
    review.review("text", "MANUSCRIPT_001", "gemini-test", sleep=sleeps.append)
    assert seen == [FAKE_KEY]


USER_KEY = "AIzaUSERkeyUSERkeyUSERkeyUSERkeyUSER12"


@pytest.mark.parametrize(("user", "server", "expected"), [
    (USER_KEY, FAKE_KEY, (USER_KEY, "user")),
    (None, FAKE_KEY, (FAKE_KEY, "server")),
    (USER_KEY, None, (USER_KEY, "user")),
    ("  \n", FAKE_KEY, (FAKE_KEY, "server")),  # a blank user key does not mask the server's
    (None, None, ("", "")),
])
def test_user_key_takes_precedence_over_server_key(monkeypatch, sleeps, user, server, expected):
    for env, value in (("BARS_USER_GEMINI_API_KEY", user), ("BARS_GEMINI_API_KEY", server)):
        if value is not None:
            monkeypatch.setenv(env, value)
    assert review.resolve_key() == expected
    assert sorted(review.env_keys()) == sorted(k for k in (user, server) if k and k.strip())
    if expected[0]:
        seen = []
        monkeypatch.setattr(review, "_make_client", lambda key: seen.append(key) or FakeClient(make_response()))
        review.review("text", "MANUSCRIPT_001", "gemini-test", sleep=sleeps.append)
        assert seen == [expected[0]]


def test_real_client_is_constructed_without_network():
    client = review._make_client(FAKE_KEY)
    assert hasattr(client.models, "generate_content")


def assert_key_free(err: BaseException):
    rendered = "".join(traceback.format_exception(err))
    assert FAKE_KEY not in str(err)
    assert FAKE_KEY not in repr(err)
    assert FAKE_KEY not in rendered
    assert err.__cause__ is None and err.__suppress_context__


@pytest.mark.parametrize("leaky", [
    errors.ClientError(400, {"error": {"code": 400, "message": f"API key {FAKE_KEY} not valid"}}),
    errors.ServerError(503, {"error": {"code": 503, "message": f"key={FAKE_KEY}"}}),
    RuntimeError(f"https://generativelanguage.googleapis.com/v1beta/models?key={FAKE_KEY}"),
])
def test_exceptions_never_contain_the_key(monkeypatch, sleeps, leaky):
    monkeypatch.setenv("BARS_GEMINI_API_KEY", FAKE_KEY)
    client = FakeClient(*[leaky] * 5)
    with pytest.raises(ReviewError) as info:
        call(client, sleeps)
    assert "[API_KEY]" in str(info.value)
    assert_key_free(info.value)


def test_client_construction_error_is_key_free(monkeypatch):
    def boom(key):
        raise RuntimeError(f"bad key {key}")

    monkeypatch.setattr(review, "_make_client", boom)
    with pytest.raises(ReviewError) as info:
        review.review("text", "MANUSCRIPT_001", "gemini-test", api_key=FAKE_KEY)
    assert_key_free(info.value)


def test_mock_mode_needs_no_key_or_network(monkeypatch):
    monkeypatch.setenv("BARS_MOCK", "1")

    class Exploding:
        @property
        def models(self):
            raise AssertionError("mock mode must not touch the client")

    result = review.review("Some scrubbed text.", "MANUSCRIPT_003", "gemini-test", client=Exploding())
    assert result.mock and result.model_version == "mock" and result.model == "gemini-test"
    assert result.markdown.startswith("# Review of MANUSCRIPT_003")
    again = review.review("Some scrubbed text.", "MANUSCRIPT_003", "gemini-test")
    assert again.markdown == result.markdown  # deterministic

    scores = parse_scores(result.markdown)
    assert scores.source == "json" and scores.issues == []
    assert (scores.rigor, scores.claims, scores.biology, scores.reproducibility) == (2.6, 1.8, 1.6, 1.2)
    assert (scores.total, scores.core, scores.tier) == (7.2, 6.0, "Strong / Sound")

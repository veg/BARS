"""Values that are written down in more than one place must agree: macros.xml tokens and
requirements, the package constants, the CI and README pins, the rubric's tier table, and the
files shipped in the tool's package directory."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from bars import __version__, cli, parse, review

REPO = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO / "galaxy" / "bars_review"
MACROS = ET.parse(TOOL_DIR / "macros.xml").getroot()


def token(name: str) -> str:
    (element,) = [t for t in MACROS.iter("token") if t.get("name") == f"@{name}@"]
    return element.text.strip()


def requirements() -> dict[str, str]:
    return {r.text.strip(): r.get("version") for r in MACROS.iter("requirement")}


def pip_pins(text: str) -> dict[str, str]:
    return dict(re.findall(r"\b([A-Za-z][\w.-]*)==([\w.]+)", text))


def test_macro_tokens_follow_the_package():
    assert token("TOOL_VERSION") == __version__
    assert token("MODEL") == cli.DEFAULT_MODEL
    assert int(token("MAX_PAPERS")) == cli.DEFAULT_MAX_PAPERS


@pytest.mark.parametrize("path", [".github/workflows/bars.yml", "galaxy/README.md"])
def test_pip_pins_match_the_tool_requirements(path):
    pins = pip_pins((REPO / path).read_text(encoding="utf-8"))
    for package, version in requirements().items():
        if package != "python":
            assert pins.get(package) == version, (path, package)


def test_tier_table_matches_the_parser():
    template = (TOOL_DIR / "bars" / "REVIEW_TEMPLATE.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*\*\*(\d+\.\d)\s*[–-]\s*\d+\.\d\*\*\s*\|\s*\*\*([^*]+)\*\*", template, re.M)
    assert [name for _, name in rows] == [name for _, name in parse.TIERS]
    assert [float(lower) for lower, _ in rows[:-1]] == [lower for lower, _ in parse.TIERS[:-1]]


def test_package_directory_ships_only_code_and_the_template():
    stray = [p.name for p in (TOOL_DIR / "bars").iterdir()
             if not (p.suffix == ".py" or p.name == "REVIEW_TEMPLATE.md" or (p.is_dir() and p.name == "__pycache__"))]
    assert stray == [], f"unexpected files in the shipped package: {stray}"


def test_user_credential_injects_the_variable_the_package_reads():
    secrets = [s for c in MACROS.iter("credentials") for s in c.iter("secret")]
    assert [s.get("inject_as_env") for s in secrets] == [review.USER_KEY_ENV]
    assert all(s.get("optional") == "true" for s in secrets)
    assert review.KEY_ENVS == (review.USER_KEY_ENV, review.KEY_ENV)

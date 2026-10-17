"""Release checks: tag script, README headings and README claims."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TAG_SCRIPT = ROOT / "scripts" / "check_tag.py"
IDENT_SCRIPT = ROOT / "scripts" / "check_identifiers.py"
README = ROOT / "README.md"
MANIFEST = ROOT / "custom_components" / "aosmith_ble" / "manifest.json"

HEADINGS = [
    "Installation",
    "Bluetooth proxy note",
    "Pairing",
    "Entities",
    "Mode mapping",
    "Day counts",
    "Repairs and recovery",
    "Polling and backoff",
    "Unsupported devices",
    "Debug logging",
    "Known limits",
]


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def _version() -> str:
    return json.loads(MANIFEST.read_text())["version"]


def test_check_tag_accepts_matching_tag() -> None:
    assert _run(TAG_SCRIPT, f"v{_version()}").returncode == 0


@pytest.mark.parametrize("tag", ["v9.9.9", _version(), "v", "", "vv0.1.0"])
def test_check_tag_rejects_mismatch(tag: str) -> None:
    assert _run(TAG_SCRIPT, tag).returncode != 0


def test_check_tag_requires_one_argument() -> None:
    assert _run(TAG_SCRIPT).returncode != 0


def test_readme_has_section_headings() -> None:
    headings = re.findall(r"^#{1,6} (.+)$", README.read_text(), flags=re.M)
    for wanted in HEADINGS:
        assert wanted in headings, wanted


def test_readme_has_non_affiliation_notice() -> None:
    text = " ".join(README.read_text().split())
    assert "not affiliated" in text
    assert "trademark" in text


def test_metadata_does_not_claim_to_be_official() -> None:
    texts = [MANIFEST.read_text(), (ROOT / "hacs.json").read_text()]
    texts.append((MANIFEST.parent / "translations" / "en.json").read_text())
    for text in texts:
        assert not re.search(r"\bofficial\b", text, re.I)


def test_readme_day_count_note() -> None:
    text = " ".join(README.read_text().split())
    assert "does not change a timed mode that is already running" in text


PROXY_CLAIM = re.compile(
    r"expected to work|(?<!un)supported|works? with|\bworks?\b|compatible",
    re.I,
)


def _proxy_claims(text: str) -> list[str]:
    """Paragraphs that mention proxies and make an unqualified support claim."""
    bad = []
    for para in text.split("\n\n"):
        flat = " ".join(para.split())
        mentions_proxy = "proxy" in flat.lower() or "proxies" in flat.lower()
        if not (mentions_proxy and PROXY_CLAIM.search(flat)):
            continue
        # Allowed: "may work", always next to an untested or unverified qualifier.
        if (
            re.search(
                r"expected to work|(?<!un)supported|works? with|compatible", flat, re.I
            )
            or re.search(r"(?<!may )\bworks?\b", flat, re.I)
            or not re.search(r"untested|unverified", flat, re.I)
        ):
            bad.append(flat)
    return bad


@pytest.mark.parametrize(
    "sample",
    [
        "ESPHome proxies are expected to work.",
        "ESPHome proxies are supported.",
        "The integration works with an ESPHome proxy.",
        "A proxy works fine.",
        "Proxies work. Local adapters are untested.",
        "Tested with one proxy, so proxies are compatible. Others are untested.",
    ],
)
def test_proxy_claim_check_catches_claims(sample: str) -> None:
    assert _proxy_claims(sample)


def test_proxy_claim_check_allows_untested_wording() -> None:
    ok = "Proxies may work but are untested, and the result is unverified."
    assert not _proxy_claims(ok)


def test_readme_makes_no_proxy_support_claim() -> None:
    assert _proxy_claims(README.read_text()) == []


def test_readme_proxy_note_says_unverified() -> None:
    assert re.search(r"unverified", README.read_text(), re.I)


def test_readme_proxy_note_names_only_the_tested_proxy() -> None:
    text = " ".join(README.read_text().split())
    assert "Tested with one ESPHome active proxy" in text
    assert "A passive proxy (0 connection slots) gives no discovery card" in text


def test_readme_passes_identifier_check() -> None:
    assert _run(IDENT_SCRIPT, str(README)).returncode == 0

"""Application versions must match the package metadata."""
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SEMVER = r"(\d+\.\d+\.\d+)"

# (relative path, regex with one capture group for the version)
VERSION_SITES = [
    ("src/api/main.py", rf'version="{SEMVER}"'),          # FastAPI app.version
]


def _pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


@pytest.mark.parametrize("rel_path,pattern", VERSION_SITES)
def test_version_matches_pyproject(rel_path, pattern):
    canonical = _pyproject_version()
    text = (ROOT / rel_path).read_text()
    m = re.search(pattern, text)
    assert m, f"no version string matching {pattern!r} found in {rel_path}"
    assert m.group(1) == canonical, (
        f"{rel_path} declares {m.group(1)} but pyproject.toml is {canonical} — "
        f"update package and API versions together"
    )

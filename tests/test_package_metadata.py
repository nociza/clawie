from __future__ import annotations

import sys
from pathlib import Path

from clawie.providers import PROVIDERS
from clawie.store import DEFAULT_CONFIG

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib


def test_package_metadata_matches_current_production_readiness() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = payload["project"]
    classifiers = set(project["classifiers"])

    assert project["license"] == "Apache-2.0"
    assert not any(classifier.startswith("License ::") for classifier in classifiers)
    assert "Development Status :: 3 - Alpha" in classifiers
    assert "Development Status :: 4 - Beta" not in classifiers
    assert "Development Status :: 5 - Production/Stable" not in classifiers
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Operating System :: OS Independent" not in classifiers
    assert not any("MacOS" in classifier or "Microsoft :: Windows" in classifier for classifier in classifiers)


def test_runtime_defaults_do_not_ship_placeholder_api_endpoints() -> None:
    assert DEFAULT_CONFIG["api_url"] == ""
    for spec in PROVIDERS.values():
        assert spec.default_api_url == ""

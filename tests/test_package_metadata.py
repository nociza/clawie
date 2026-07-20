from __future__ import annotations

import sys
from pathlib import Path

from clawie.providers import PROVIDERS
from clawie.ipc_paths import control_socket_path
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
    assert "Development Status :: 5 - Production/Stable" not in classifiers
    assert "Development Status :: 3 - Alpha" not in classifiers
    assert "Development Status :: 4 - Beta" in classifiers
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Operating System :: OS Independent" not in classifiers
    assert not any("MacOS" in classifier or "Microsoft :: Windows" in classifier for classifier in classifiers)
    assert "Programming Language :: Python :: 3.14" in classifiers


def test_runtime_defaults_do_not_ship_placeholder_api_endpoints() -> None:
    assert DEFAULT_CONFIG["api_url"] == ""
    for spec in PROVIDERS.values():
        assert spec.default_api_url == ""


def test_verified_openclaw_runtime_install_is_version_pinned() -> None:
    assert PROVIDERS["openclaw"].install_package == "openclaw@2026.7.1"


def test_control_socket_paths_are_manager_scoped() -> None:
    first = control_socket_path("/var/lib/clawie-a", 1001)
    second = control_socket_path("/var/lib/clawie-b", 1001)

    assert first.parent == Path("/run/clawie/control")
    assert first.name.startswith("1001-")
    assert first.suffix == ".sock"
    assert first != second

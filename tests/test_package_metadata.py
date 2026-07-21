from __future__ import annotations

import re
import sys
from pathlib import Path

from clawie.providers import PROVIDERS
from clawie.ipc_paths import control_socket_path, delegation_socket_path
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
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert "Development Status :: 3 - Alpha" not in classifiers
    assert "Development Status :: 4 - Beta" not in classifiers
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

    delegated = delegation_socket_path("/var/lib/clawie-a", 1001)
    assert delegated.parent == Path("/run/clawie/control")
    assert delegated.name.startswith("delegation-1001-")
    assert delegated.suffix == ".sock"
    assert delegated != first


def test_release_workflow_is_pinned_and_uses_trusted_publishing() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    action_refs = re.findall(r"^\s*uses:\s*[^@\s]+@([^\s#]+)", workflow, flags=re.MULTILINE)

    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
    assert "types: [published]" in workflow
    assert "name: pypi" in workflow
    assert "id-token: write" in workflow
    assert "PYPI_TOKEN" not in workflow
    assert "github.event.release.tag_name" in workflow
    assert "uv audit --frozen" in workflow


def test_production_fixture_exercises_the_real_public_user_journey() -> None:
    fixture = Path("scripts/production_verify_fixture.py").read_text(encoding="utf-8")

    assert '"runtime",\n                "install",\n                "openclaw"' in fixture
    assert '"runtime",\n                    "create"' in fixture
    assert '"agent",\n                    "service",\n                    "start"' in fixture
    assert '"--exercise-runtime-delivery"' in fixture
    assert '"agent",\n                        "purge"' in fixture
    assert 'proof_payload["cleanup"] = cleanup' in fixture
    assert '"user_absent": absent' in fixture
    assert '"preserved_state_root"' in fixture
    assert "wrapper_dir = _trusted_wrapper_dir(version_token)" in fixture
    assert 'dir=home' in fixture
    assert "_seed_state" not in fixture
    assert "_create_user_with_private_auth" not in fixture
    assert 'required=True,\n        help="Home containing real provider or linked auth' in fixture

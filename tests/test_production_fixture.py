from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.production_verify_fixture import (
    _trusted_wrapper_dir,
    _validated_linked_auth_evidence,
)


def test_production_fixture_requires_live_codex_account_status() -> None:
    evidence = _validated_linked_auth_evidence(
        """{
          "auth": [{
            "provider": "openclaw",
            "auth_mode": "linked",
            "auth_status": "ready",
            "auth_profile": "openai-codex:default",
            "account": "acct-secret",
            "source": "cli",
            "login_required": false
          }]
        }""",
        provider="openclaw",
        require_account=True,
    )

    assert evidence == {
        "provider": "openclaw",
        "auth_mode": "linked",
        "auth_status": "ready",
        "auth_profile": "openai-codex:default",
        "account_present": True,
        "source": "cli",
        "login_required": False,
    }
    assert "acct-secret" not in str(evidence)


@pytest.mark.parametrize(
    ("auth_status", "source", "account", "message"),
    [
        ("missing", "cli", "acct-1", "not confirmed ready"),
        ("ready", "file:auth.json", "acct-1", "not confirmed ready"),
        ("ready", "cli", "", "did not report a Codex account"),
    ],
)
def test_production_fixture_rejects_unproven_codex_login(
    auth_status: str,
    source: str,
    account: str,
    message: str,
) -> None:
    payload = {
        "auth": [
            {
                "provider": "openclaw",
                "auth_mode": "linked",
                "auth_status": auth_status,
                "account": account,
                "source": source,
            }
        ]
    }

    with pytest.raises(RuntimeError, match=message):
        _validated_linked_auth_evidence(
            json.dumps(payload),
            provider="openclaw",
            require_account=True,
        )


def test_production_wrapper_is_created_below_trusted_effective_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_home = tmp_path / "root-home"
    trusted_home.mkdir(mode=0o700)
    monkeypatch.setattr(
        "scripts.production_verify_fixture.pwd.getpwuid",
        lambda _uid: type("PwdRow", (), {"pw_dir": str(trusted_home)})(),
    )

    wrapper_dir = _trusted_wrapper_dir("018")

    assert wrapper_dir.parent == trusted_home.resolve()
    assert wrapper_dir.stat().st_uid == os.geteuid()
    assert wrapper_dir.stat().st_mode & 0o077 == 0


def test_production_wrapper_rejects_writable_effective_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_home = tmp_path / "unsafe-root-home"
    unsafe_home.mkdir(mode=0o700)
    unsafe_home.chmod(0o777)
    monkeypatch.setattr(
        "scripts.production_verify_fixture.pwd.getpwuid",
        lambda _uid: type("PwdRow", (), {"pw_dir": str(unsafe_home)})(),
    )

    with pytest.raises(RuntimeError, match="not group/world-writable"):
        _trusted_wrapper_dir("018")

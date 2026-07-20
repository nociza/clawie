#!/usr/bin/env python3
"""Provision a disposable production journey from a wheel and verify it.

This script is intended for release proof generation on the target
Linux/systemd host.  It uses only public ``clawie`` commands to configure a
fresh state root, import real linked auth, provision two isolated runtimes,
start their services, exercise live delivery and watchdog recovery, and purge
the fixture.  Synthetic agent records and placeholder credentials are not
accepted as production evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


WATCHDOG_UNIT = Path("/etc/systemd/system/clawie-control-watchdog.service")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, help="Path to the built clawie wheel")
    parser.add_argument("--version", required=True, help="Release version under proof")
    parser.add_argument(
        "--source-home",
        required=True,
        help="Home containing real provider or linked auth to copy into the disposable runtimes",
    )
    parser.add_argument(
        "--auth-source",
        choices=("codex", "provider", "claude"),
        default="codex",
        help="Credential format to import from --source-home (default: codex)",
    )
    parser.add_argument("--interval", type=int, default=1, help="Watchdog interval seconds")
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        raise SystemExit("production proof fixture requires root")
    wheel = Path(args.wheel).expanduser().resolve()
    if not wheel.is_file():
        raise SystemExit(f"wheel not found: {wheel}")
    source_home = Path(args.source_home).expanduser().resolve()
    if not source_home.is_dir():
        raise SystemExit(f"source home not found: {source_home}")
    if WATCHDOG_UNIT.exists() or WATCHDOG_UNIT.is_symlink():
        raise SystemExit(f"refusing to replace existing watchdog unit: {WATCHDOG_UNIT}")

    version = str(args.version).strip()
    version_token = re.sub(r"[^0-9a-z]", "", version.lower()) or "release"
    state_root = Path(tempfile.mkdtemp(prefix=f"clawie-release-proof-{version_token}-"))
    wrapper_dir = Path(tempfile.mkdtemp(prefix=f"clawie-wheel-proof-bin-{version_token}-"))
    nonce = re.sub(r"[^0-9a-z]", "", state_root.name.rsplit("-", 1)[-1].lower())
    suffix = f"{version_token}{nonce}"[-20:]
    users = [f"clawier{suffix}a", f"clawier{suffix}b"]
    agent_rows: list[dict[str, str]] = []
    created_agents: list[str] = []
    created_users: dict[str, str] = {}
    attempted_agents: list[str] = []
    env = os.environ.copy()
    proof_payload: dict[str, Any] | None = None
    cleanup: dict[str, Any] = {"watchdog_removed": False, "users_removed": [], "paths_removed": []}

    try:
        for user in users:
            if _run(["id", "-u", user], check=False).returncode == 0:
                raise RuntimeError(f"refusing production fixture username collision: {user}")
        _write_wrapper(wrapper_dir, wheel)
        env["PATH"] = f"{wrapper_dir}:{env.get('PATH', '')}"
        expected_version = f"clawie {version}"
        installed_version = _run(["clawie", "--version"], env=env).stdout.strip()
        if installed_version != expected_version:
            raise RuntimeError(
                f"wheel version mismatch: expected {expected_version!r}, got {installed_version!r}"
            )

        _run(
            [
                "clawie",
                "--no-color",
                "--config-dir",
                str(state_root),
                "config",
                "set",
                "--provider",
                "openclaw",
                "--auth-mode",
                "linked",
                "--subscription",
                "pro",
                "--workspace",
                "production-proof",
            ],
            env=env,
        )
        _run(
            [
                "clawie",
                "--no-color",
                "--config-dir",
                str(state_root),
                "auth",
                "import",
                "openclaw",
                "--from",
                str(args.auth_source),
                "--source-home",
                str(source_home),
            ],
            env=env,
        )

        for idx, user in enumerate(users):
            agent_id = f"release-{'a' if idx == 0 else 'b'}"
            attempted_agents.append(agent_id)
            created_users[agent_id] = user
            _run(
                [
                    "clawie",
                    "--no-color",
                    "--config-dir",
                    str(state_root),
                    "runtime",
                    "create",
                    agent_id,
                    "--user",
                    user,
                    "--provider",
                    "openclaw",
                    "--source-home",
                    str(source_home),
                    "--credential-bundle",
                    "provider-auth",
                    "--no-global-password",
                ],
                env=env,
            )
            created_agents.append(agent_id)
            _run(
                [
                    "clawie",
                    "--no-color",
                    "--config-dir",
                    str(state_root),
                    "agent",
                    "service",
                    "start",
                    agent_id,
                ],
                env=env,
            )
            agent_rows.append(
                {
                    "agent_id": agent_id,
                    "linux_user": user,
                    "home": f"/home/{user}",
                    "service": "started",
                }
            )

        preflight_install = _run(
            [
                "clawie",
                "--no-color",
                "--config-dir",
                str(state_root),
                "control",
                "watchdog",
                "install",
                "--interval",
                str(int(args.interval)),
            ],
            env=env,
        )
        preflight = {
            "install_stdout": preflight_install.stdout,
            "active": _run(["systemctl", "is-active", WATCHDOG_UNIT.name], check=False).stdout.strip(),
            "enabled": _run(["systemctl", "is-enabled", WATCHDOG_UNIT.name], check=False).stdout.strip(),
            "show": _run(
                [
                    "systemctl",
                    "show",
                    WATCHDOG_UNIT.name,
                    "--property=Restart,MainPID,NRestarts,ActiveState,SubState",
                ],
                check=False,
            ).stdout.strip(),
            "unit_exec_start": _unit_exec_start(WATCHDOG_UNIT),
        }

        verify = _run(
            [
                "clawie",
                "--no-color",
                "--config-dir",
                str(state_root),
                "production",
                "verify",
                "--exercise-watchdog-restart",
                "--exercise-runtime-delivery",
                "--all-provider-contracts",
                "--json",
            ],
            env=env,
            check=False,
        )
        try:
            result_payload = json.loads(verify.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"production verify did not emit JSON: {verify.stdout!r}") from exc
        if verify.returncode != 0 or result_payload.get("status") != "passed":
            raise RuntimeError(f"production verify failed with exit {verify.returncode}")

        proof_payload = {
            "version": version,
            "wheel": str(wheel),
            "wheel_sha256": _sha256(wheel),
            "installed_version": installed_version,
            "auth_source": str(args.auth_source),
            "state_root": str(state_root),
            "wrapper": str(wrapper_dir / "clawie"),
            "agents": agent_rows,
            "preflight": preflight,
            "result": result_payload,
        }
    finally:
        if (wrapper_dir / "clawie").is_file():
            remove = _run(
                [
                    "clawie",
                    "--no-color",
                    "--config-dir",
                    str(state_root),
                    "control",
                    "watchdog",
                    "remove",
                ],
                env=env,
                check=False,
            )
            cleanup["watchdog_remove_command_ok"] = remove.returncode == 0
            cleanup["watchdog_removed"] = not (
                WATCHDOG_UNIT.exists() or WATCHDOG_UNIT.is_symlink()
            )
            for agent_id in reversed(attempted_agents):
                removed = _run(
                    [
                        "clawie",
                        "--no-color",
                        "--config-dir",
                        str(state_root),
                        "agent",
                        "purge",
                        agent_id,
                        "--yes",
                    ],
                    env=env,
                    check=False,
                )
                linux_user = created_users[agent_id]
                absent = _run(["id", "-u", linux_user], check=False).returncode != 0
                cleanup["users_removed"].append(
                    {
                        "agent_id": agent_id,
                        "linux_user": linux_user,
                        "runtime_created": agent_id in created_agents,
                        "command_ok": removed.returncode == 0,
                        "user_absent": absent,
                        "ok": absent,
                    }
                )
        else:
            cleanup["watchdog_removed"] = not (
                WATCHDOG_UNIT.exists() or WATCHDOG_UNIT.is_symlink()
            )

        cleanup_ok = bool(cleanup["watchdog_removed"]) and all(
            bool(row.get("ok", False))
            for row in cleanup["users_removed"]
            if isinstance(row, dict)
        )
        cleanup["ok"] = cleanup_ok
        if cleanup_ok:
            for path in [state_root, wrapper_dir]:
                shutil.rmtree(path, ignore_errors=True)
                path_removed = not path.exists()
                cleanup["paths_removed"].append({"path": str(path), "removed": path_removed})
                cleanup_ok = cleanup_ok and path_removed
            cleanup["ok"] = cleanup_ok
        else:
            cleanup["preserved_state_root"] = str(state_root)
            cleanup["preserved_wrapper"] = str(wrapper_dir / "clawie")
        if proof_payload is None:
            print(json.dumps({"cleanup": cleanup}, indent=2, sort_keys=True), file=sys.stderr)

    if not bool(cleanup.get("ok", False)):
        print(
            json.dumps({"proof": proof_payload, "cleanup": cleanup}, indent=2, sort_keys=True),
            file=sys.stderr,
        )
        raise RuntimeError(
            "production proof completed but fixture cleanup failed; recovery paths were preserved"
        )
    assert proof_payload is not None
    proof_payload["cleanup"] = cleanup
    print(json.dumps(proof_payload, indent=2, sort_keys=True))
    return 0


def _write_wrapper(wrapper_dir: Path, wheel: Path) -> None:
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "clawie"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"export PYTHONPATH={shlex.quote(str(wheel))}\n"
        'exec /usr/bin/env python3 -m clawie "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unit_exec_start(path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            return line
    return ""


def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if check and result.returncode != 0:
        output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part.strip())
        raise RuntimeError(f"{cmd!r} failed with exit {result.returncode}: {output}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())

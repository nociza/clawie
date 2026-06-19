#!/usr/bin/env python3
"""Create a disposable Linux/systemd fixture and run clawie production verify.

This script is intended for release proof generation inside a Linux VM. It
expects to run as root, points a temporary ``clawie`` wrapper at a built wheel,
creates two disposable Linux users with private provider-auth files, installs
the watchdog, runs the aggregate verifier, and removes the fixture.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


WATCHDOG_UNIT = Path("/etc/systemd/system/clawie-control-watchdog.service")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, help="Path to the built clawie wheel")
    parser.add_argument("--version", required=True, help="Release version under proof")
    parser.add_argument("--interval", type=int, default=1, help="Watchdog interval seconds")
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        raise SystemExit("production proof fixture requires root")
    wheel = Path(args.wheel)
    if not wheel.exists():
        raise SystemExit(f"wheel not found: {wheel}")
    if WATCHDOG_UNIT.exists():
        raise SystemExit(f"refusing to replace existing watchdog unit: {WATCHDOG_UNIT}")

    version = str(args.version).strip()
    version_token = re.sub(r"[^0-9a-z]", "", version.lower()) or "release"
    stamp = time.strftime("%H%M%S")
    suffix = f"{version_token}{stamp}"[-20:]
    state_root = Path(f"/tmp/clawie-colima-release-proof-{version_token}-{stamp}")
    wrapper_dir = Path(f"/tmp/clawie-wheel-proof-bin-{version_token}-{stamp}")
    users = [f"clawier{suffix}a", f"clawier{suffix}b"]
    agent_rows: list[dict[str, str]] = []
    env = os.environ.copy()
    result_payload: dict[str, Any] | None = None
    cleanup: dict[str, Any] = {"watchdog_removed": False, "users_removed": [], "paths_removed": []}

    try:
        _write_wrapper(wrapper_dir, wheel)
        env["PATH"] = f"{wrapper_dir}:{env.get('PATH', '')}"
        _seed_state(state_root, users)
        for idx, user in enumerate(users):
            agent_id = f"release-{'a' if idx == 0 else 'b'}"
            home = _create_user_with_private_auth(user)
            agent_rows.append({"agent_id": agent_id, "linux_user": user, "home": str(home)})

        preflight_install = _run(
            [
                "clawie",
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
                "--config-dir",
                str(state_root),
                "production",
                "verify",
                "--exercise-watchdog-restart",
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

        print(
            json.dumps(
                {
                    "version": version,
                    "wheel": str(wheel),
                    "state_root": str(state_root),
                    "wrapper": str(wrapper_dir / "clawie"),
                    "agents": agent_rows,
                    "preflight": preflight,
                    "result": result_payload,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        remove = _run(
            [
                "clawie",
                "--config-dir",
                str(state_root),
                "control",
                "watchdog",
                "remove",
            ],
            env=env,
            check=False,
        )
        cleanup["watchdog_removed"] = remove.returncode == 0
        for user in users:
            removed = _run(["userdel", "-r", user], check=False)
            cleanup["users_removed"].append({"user": user, "ok": removed.returncode == 0})
        for path in [state_root, wrapper_dir]:
            shutil.rmtree(path, ignore_errors=True)
            cleanup["paths_removed"].append(str(path))
        if result_payload is None:
            print(json.dumps({"cleanup": cleanup}, indent=2, sort_keys=True), file=sys.stderr)


def _seed_state(state_root: Path, users: list[str]) -> None:
    from clawie.service import ClawieService
    from clawie.store import StateStore

    service = ClawieService(StateStore(config_dir=state_root))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="pro",
        workspace="production",
        api_url="",
    )
    state = service.store.read_state()
    agents: dict[str, Any] = {}
    for idx, user in enumerate(users):
        agent_id = f"release-{'a' if idx == 0 else 'b'}"
        agents[agent_id] = {
            "agent_id": agent_id,
            "display_name": agent_id,
            "channels": [{"id": "proof", "type": "proof", "enabled": True}],
            "agent": {
                "linux_user": user,
                "provider": "openclaw",
                "runtime": "openclaw-agent",
                "model_tier": "balanced",
                "status": "active",
                "local_user": False,
            },
            "credential_sync": {
                "bundles": ["provider-auth"],
                "shared_provider_auth": True,
                "last_synced_paths": [
                    ".openclaw/agents/main/agent/openclaw-agent.sqlite",
                ],
            },
        }
    state["agents"] = agents
    service.store.write_state(state)


def _create_user_with_private_auth(user: str) -> Path:
    _run(["useradd", "-m", "-s", "/bin/bash", user])
    home = Path(pwd.getpwnam(user).pw_dir)
    home.chmod(0o700)
    auth_file = home / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("proof\n", encoding="utf-8")
    auth_file.chmod(0o600)
    uid = pwd.getpwnam(user).pw_uid
    gid = pwd.getpwnam(user).pw_gid
    auth_paths = [
        home / ".openclaw",
        home / ".openclaw" / "agents",
        home / ".openclaw" / "agents" / "main",
        auth_file.parent,
        auth_file,
    ]
    for path in auth_paths:
        os.chown(path, uid, gid)
    return home


def _write_wrapper(wrapper_dir: Path, wheel: Path) -> None:
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "clawie"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"export PYTHONPATH={wheel}\n"
        'exec /usr/bin/python3 -m clawie "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


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

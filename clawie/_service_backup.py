"""Git-backed knowledge backup and restore (ZeroClawService mixin).

The backup repository mirrors the fleet's durable knowledge:

- ``state/snapshot.json`` — config and agent records with secrets redacted
- ``agents/<agent_id>/prompts/`` — core prompt files from the control plane
- ``agents/<agent_id>/workspace/`` — knowledge files captured from the live
  agent workspace (markdown notes, memory files)

Credential material is never written to the repository: the snapshot is
redacted, workspace collection skips symlinks and credential-looking names,
and a ``.gitignore`` safety net excludes auth file patterns.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from clawie.providers import get_provider
from clawie.service_common import SetupError, AgentNotFoundError, now_iso, redact

_BACKUP_README = """# clawie knowledge backup

This repository is maintained automatically by `clawie backup`.

Layout:

- `state/snapshot.json` — fleet config and agent records (secrets redacted)
- `agents/<agent_id>/prompts/` — core prompt files (SOUL.md, MEMORY.md, ...)
- `agents/<agent_id>/workspace/` — knowledge files captured from the agent workspace

Events are intentionally excluded so that commits only happen when knowledge
actually changes. Credentials are never written here; use `clawie backup export`
for a full-fidelity local snapshot instead.

Restore prompts with `clawie backup restore [--agent AGENT_ID]`.
"""

_BACKUP_GITIGNORE = """# Safety net: never commit credential material.
auth.json
auth-profiles.json
.credentials.json
.codex/
.openai/
*.pem
*.key
.env
.env.*
"""

# Workspace files with these substrings in their name are never collected.
_SENSITIVE_NAME_TOKENS = (
    "auth",
    "credential",
    "secret",
    "token",
    "password",
    "apikey",
    "api-key",
    "api_key",
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_KNOWLEDGE_SUFFIXES = (".md", ".markdown", ".txt")
_MAX_KNOWLEDGE_FILE_BYTES = 1024 * 1024
_MAX_KNOWLEDGE_FILES_PER_AGENT = 1000
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Workspace knowledge file names may contain spaces; still no leading dots,
# path separators, or shell-hostile characters.
_SAFE_WORKSPACE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


class BackupOpsMixin:

    # ── settings ──────────────────────────────────────────────────────────

    def backup_settings(self) -> dict[str, Any]:
        config = self.store.read_config()
        repo = str(config.get("backup_repo_path", "")).strip()
        return {
            "enabled": bool(config.get("backup_enabled", False)),
            "repo": repo or str(self._default_backup_repo_path()),
            "repo_configured": bool(repo),
            "remote": str(config.get("backup_remote", "")).strip(),
            "auto_push": bool(config.get("backup_auto_push", True)),
            "last_run_at": str(config.get("backup_last_run_at", "")),
            "last_commit": str(config.get("backup_last_commit", "")),
        }

    def _default_backup_repo_path(self) -> Path:
        return self.store.root / "backup"

    def _backup_repo_path(self) -> Path:
        settings = self.backup_settings()
        return Path(settings["repo"]).expanduser()

    # ── lifecycle ─────────────────────────────────────────────────────────

    def backup_init(
        self,
        repo_path: str | Path | None = None,
        *,
        remote: str | None = None,
        enable: bool = True,
    ) -> dict[str, Any]:
        """Create (or adopt) the backup git repository and record its settings."""
        self._require_backup_git()
        config = self.store.read_config()
        if repo_path:
            repo = Path(repo_path).expanduser()
        else:
            configured = str(config.get("backup_repo_path", "")).strip()
            repo = Path(configured).expanduser() if configured else self._default_backup_repo_path()
        # Anchor relative paths to the current directory now; the maintenance
        # cron runs from a different cwd and must find the same repo.
        repo = repo.resolve()
        repo.mkdir(parents=True, exist_ok=True)

        created = not (repo / ".git").exists()
        if created:
            self._run_backup_git(repo, "init", "--quiet")

        readme = repo / "README.md"
        if not readme.exists():
            readme.write_text(_BACKUP_README, encoding="utf-8")
        gitignore = repo / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_BACKUP_GITIGNORE, encoding="utf-8")

        remote_url = str(remote or "").strip()
        if remote_url:
            has_origin = (
                self._run_backup_git(repo, "remote", "get-url", "origin", check=False).returncode == 0
            )
            if has_origin:
                self._run_backup_git(repo, "remote", "set-url", "origin", remote_url)
            else:
                self._run_backup_git(repo, "remote", "add", "origin", remote_url)

        config["backup_repo_path"] = str(repo)
        if remote_url:
            config["backup_remote"] = remote_url
        config["backup_enabled"] = bool(enable)
        self.store.write_config(config)
        self._restore_backup_repo_ownership(repo)

        state = self.store.read_state()
        self._event(
            state,
            "backup.initialized",
            f"Backup repo initialized at {repo}",
            {"repo": str(repo), "remote": remote_url, "enabled": bool(enable), "created": created},
        )
        self.store.write_state(state)
        return {
            "repo": str(repo),
            "remote": remote_url or str(config.get("backup_remote", "")).strip(),
            "enabled": bool(enable),
            "created": created,
        }

    def backup_run(self, *, message: str = "", push: bool | None = None) -> dict[str, Any]:
        """Mirror fleet knowledge into the backup repo and commit if changed."""
        self._require_backup_git()
        repo = self._backup_repo_path()
        if not (repo / ".git").exists():
            # First run bootstraps the repo so automatic backups Just Work.
            self.backup_init(repo, enable=bool(self.backup_settings()["enabled"]))

        result = self._write_backup_tree(repo)
        self._run_backup_git(repo, "add", "-A")
        dirty = bool(self._run_backup_git(repo, "status", "--porcelain").stdout.strip())

        commit = ""
        if dirty:
            commit_message = str(message).strip() or f"clawie backup {now_iso()}"
            self._run_backup_git(repo, "commit", "--quiet", "-m", commit_message)
        head = self._run_backup_git(repo, "rev-parse", "HEAD", check=False)
        if head.returncode == 0:
            commit = head.stdout.strip()

        settings = self.backup_settings()
        should_push = settings["auto_push"] if push is None else bool(push)
        pushed = False
        push_error = ""
        if should_push and settings["remote"] and commit:
            outcome = self._run_backup_git(repo, "push", "-u", "origin", "HEAD", check=False)
            if outcome.returncode == 0:
                pushed = True
            else:
                push_error = (outcome.stderr or outcome.stdout or "").strip() or (
                    f"git push exited {outcome.returncode}"
                )

        config = self.store.read_config()
        config["backup_last_run_at"] = now_iso()
        if commit:
            config["backup_last_commit"] = commit
        self.store.write_config(config)
        self._restore_backup_repo_ownership(repo)

        state = self.store.read_state()
        self._event(
            state,
            "backup.completed",
            f"Backup run: {'committed ' + commit[:10] if dirty else 'no changes'}",
            {
                "repo": str(repo),
                "changed": dirty,
                "commit": commit,
                "pushed": pushed,
                "push_error": push_error,
                "agents": result["agents"],
                "files": result["files"],
                "skipped": result["skipped"],
            },
        )
        self.store.write_state(state)
        return {
            "repo": str(repo),
            "changed": dirty,
            "commit": commit,
            "pushed": pushed,
            "push_error": push_error,
            "agents": result["agents"],
            "files": result["files"],
            "skipped": result["skipped"],
        }

    def backup_status(self) -> dict[str, Any]:
        """Read-only view of backup configuration and repository state."""
        settings = self.backup_settings()
        repo = Path(settings["repo"]).expanduser()
        payload: dict[str, Any] = {
            "enabled": settings["enabled"],
            "repo": str(repo),
            "remote": settings["remote"],
            "auto_push": settings["auto_push"],
            "last_run_at": settings["last_run_at"],
            "last_commit": settings["last_commit"],
            "git_available": bool(shutil.which("git")),
            "initialized": (repo / ".git").exists(),
            "dirty": False,
            "head": "",
            "commit_count": 0,
        }
        if not payload["git_available"] or not payload["initialized"]:
            return payload
        status = self._run_backup_git(repo, "status", "--porcelain", check=False)
        if status.returncode == 0:
            payload["dirty"] = bool(status.stdout.strip())
        head = self._run_backup_git(repo, "rev-parse", "--short", "HEAD", check=False)
        if head.returncode == 0:
            payload["head"] = head.stdout.strip()
        count = self._run_backup_git(repo, "rev-list", "--count", "HEAD", check=False)
        if count.returncode == 0:
            try:
                payload["commit_count"] = int(count.stdout.strip())
            except ValueError:
                pass
        return payload

    def backup_restore(
        self,
        agent_id: str | None = None,
        *,
        apply_to_disk: bool = True,
        include_workspace: bool = True,
    ) -> dict[str, Any]:
        """Restore agent prompts (and optionally workspace knowledge) from the backup repo."""
        repo = self._backup_repo_path()
        agents_root = repo / "agents"
        if not agents_root.is_dir():
            raise SetupError(
                f"backup repo has no agents to restore (looked at: {agents_root}). "
                "Run 'clawie backup run' first."
            )

        state = self.store.read_state()
        known_agents = state.setdefault("agents", state.get("users", {}))
        requested = str(agent_id or "").strip()
        if requested:
            if not (agents_root / requested).is_dir():
                raise AgentNotFoundError(f"agent not found in backup repo: {requested}")
            if requested not in known_agents:
                raise AgentNotFoundError(
                    f"agent '{requested}' exists in the backup but not in local state. "
                    "Create the agent (or 'clawie backup import' a state snapshot) first."
                )
            targets = [requested]
        else:
            targets = sorted(entry.name for entry in agents_root.iterdir() if entry.is_dir())

        restored: dict[str, dict[str, int]] = {}
        skipped: list[dict[str, str]] = []
        for token in targets:
            if token not in known_agents:
                skipped.append({"agent_id": token, "reason": "not in local state"})
                continue
            prompts_restored = self._restore_agent_prompts(token, agents_root / token / "prompts")
            if apply_to_disk and prompts_restored:
                try:
                    self.write_agent_core_prompts_to_disk(token)
                except (SetupError, PermissionError) as exc:
                    skipped.append({"agent_id": token, "reason": f"prompts not applied to disk: {exc}"})
            # Workspace files are restored after prompts hit the disk: the
            # captured workspace holds the live knowledge (e.g. an agent's
            # self-edited MEMORY.md) and must win over control-plane defaults.
            workspace_restored = 0
            if include_workspace:
                workspace_restored = self._restore_agent_workspace(
                    token, agents_root / token / "workspace", skipped
                )
            restored[token] = {"prompts": prompts_restored, "workspace_files": workspace_restored}

        state = self.store.read_state()
        self._event(
            state,
            "backup.restored",
            f"Restored {len(restored)} agent(s) from backup",
            {"repo": str(repo), "agents": sorted(restored), "skipped": skipped},
        )
        self.store.write_state(state)
        return {"repo": str(repo), "restored": restored, "skipped": skipped}

    # ── tree construction ────────────────────────────────────────────────

    def _write_backup_tree(self, repo: Path) -> dict[str, Any]:
        agents_backed_up: list[str] = []
        files_written = 0
        skipped: list[dict[str, str]] = []

        state_dir = repo / "state"
        agents_dir = repo / "agents"
        for stale in (state_dir, agents_dir):
            if stale.exists():
                shutil.rmtree(stale)
        state_dir.mkdir(parents=True)
        agents_dir.mkdir(parents=True)

        snapshot = {
            "config": self._redacted_backup_config(),
            "state": self._redacted_backup_state(),
        }
        (state_dir / "snapshot.json").write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        files_written += 1

        state = self.store.read_state()
        agents = state.get("agents", state.get("users", {}))
        for token, agent in sorted(agents.items()):
            if not isinstance(agent, dict):
                continue
            safe_id = str(token).strip()
            if not _SAFE_PATH_SEGMENT.fullmatch(safe_id):
                skipped.append({"agent_id": safe_id, "reason": "unsafe agent id for backup paths"})
                continue
            agent_root = agents_dir / safe_id
            provider = str(agent.get("agent", {}).get("provider", "")).strip().lower()
            files_written += self._write_agent_prompt_backups(agent_root, provider, agent)
            files_written += self._write_agent_workspace_backups(
                agent_root, provider, agent, skipped, agent_id=safe_id
            )
            agents_backed_up.append(safe_id)
        return {"agents": agents_backed_up, "files": files_written, "skipped": skipped}

    def _write_agent_prompt_backups(self, agent_root: Path, provider: str, agent: dict[str, Any]) -> int:
        prompts = agent.get("core_prompts", {})
        if not isinstance(prompts, dict):
            return 0
        names = set(self._provider_core_prompt_names(provider))
        written = 0
        for name, content in sorted(prompts.items()):
            token = str(name).strip()
            body = str(content or "")
            if token not in names or not body.strip():
                continue
            target = agent_root / "prompts" / token
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            written += 1
        return written

    def _write_agent_workspace_backups(
        self,
        agent_root: Path,
        provider: str,
        agent: dict[str, Any],
        skipped: list[dict[str, str]],
        *,
        agent_id: str,
    ) -> int:
        if not provider:
            return 0
        try:
            spec = get_provider(provider)
        except ValueError:
            return 0
        home = self._agent_linux_home(agent)
        if home is None:
            return 0
        workspace = home / spec.state_dir / spec.workspace_dir
        try:
            if not workspace.is_dir():
                return 0
        except OSError:
            return 0

        written = 0
        try:
            entries = sorted(workspace.rglob("*"), key=str)
        except (OSError, PermissionError) as exc:
            skipped.append({"agent_id": agent_id, "reason": f"workspace unreadable: {exc}"})
            return 0
        for entry in entries:
            if written >= _MAX_KNOWLEDGE_FILES_PER_AGENT:
                skipped.append(
                    {"agent_id": agent_id, "reason": "workspace file cap reached; remaining files skipped"}
                )
                break
            if not self._is_backupable_knowledge_file(entry, workspace):
                continue
            rel = entry.relative_to(workspace)
            target = agent_root / "workspace" / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry, target)
                written += 1
            except (OSError, PermissionError) as exc:
                skipped.append({"agent_id": agent_id, "reason": f"could not copy {rel}: {exc}"})
        return written

    @staticmethod
    def _is_backupable_knowledge_file(entry: Path, workspace: Path) -> bool:
        try:
            if entry.is_symlink() or not entry.is_file():
                return False
            rel = entry.relative_to(workspace)
            for segment in rel.parts:
                if segment.startswith(".") or not _SAFE_WORKSPACE_SEGMENT.fullmatch(segment):
                    return False
            name = entry.name.lower()
            if any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
                return False
            in_memory_dir = rel.parts[0] == "memory" and len(rel.parts) > 1
            if not in_memory_dir and not any(name.endswith(suffix) for suffix in _KNOWLEDGE_SUFFIXES):
                return False
            stem = name.rsplit(".", 1)[0]
            if any(token in stem for token in _SENSITIVE_NAME_TOKENS):
                return False
            if entry.stat().st_size > _MAX_KNOWLEDGE_FILE_BYTES:
                return False
        except OSError:
            return False
        return True

    def _redacted_backup_config(self) -> dict[str, Any]:
        config = copy.deepcopy(self.store.read_config())
        # Backup bookkeeping changes on every run; including it would make
        # each run dirty the repo and defeat commit-on-change detection.
        config.pop("backup_last_run_at", None)
        config.pop("backup_last_commit", None)
        if str(config.get("api_key", "")).strip():
            config["api_key"] = redact(str(config["api_key"]))
        if str(config.get("spawn_password_hash", "")).strip():
            config["spawn_password_hash"] = "<redacted>"
        credentials = config.get("provider_credentials", {})
        if isinstance(credentials, dict):
            for payload in credentials.values():
                if isinstance(payload, dict) and str(payload.get("api_key", "")).strip():
                    payload["api_key"] = redact(str(payload["api_key"]))
        return config

    def _redacted_backup_state(self) -> dict[str, Any]:
        state = copy.deepcopy(self.store.read_state())
        # Events are an append-only audit log; excluding them keeps backup
        # commits meaningful (knowledge changes only, not every cron tick).
        state.pop("events", None)
        state.pop("users", None)
        agents = state.get("agents", {})
        if isinstance(agents, dict):
            for agent in agents.values():
                if not isinstance(agent, dict):
                    continue
                channels = agent.get("channels", [])
                if not isinstance(channels, list):
                    continue
                for channel in channels:
                    if not isinstance(channel, dict):
                        continue
                    name = str(channel.get("name", ""))
                    if self._looks_like_telegram_bot_token(name):
                        channel["name"] = redact(name)
        return state

    # ── restore helpers ──────────────────────────────────────────────────

    def _restore_agent_prompts(self, agent_id: str, prompts_dir: Path) -> int:
        if not prompts_dir.is_dir():
            return 0
        payload = self.get_dashboard_agent(agent_id)
        provider = str(payload.get("agent", {}).get("provider", "")).strip().lower()
        valid_names = set(self._provider_core_prompt_names(provider))
        restored = 0
        for entry in sorted(prompts_dir.iterdir()):
            if not entry.is_file() or entry.name not in valid_names:
                continue
            content = entry.read_text(encoding="utf-8")
            self.set_agent_core_prompt(agent_id, entry.name, content, sync_to_disk=False)
            restored += 1
        return restored

    def _restore_agent_workspace(
        self,
        agent_id: str,
        workspace_backup: Path,
        skipped: list[dict[str, str]],
    ) -> int:
        if not workspace_backup.is_dir():
            return 0
        state = self.store.read_state()
        agent = state.get("agents", state.get("users", {})).get(agent_id, {})
        provider = str(agent.get("agent", {}).get("provider", "")).strip().lower()
        linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
        if not provider:
            return 0
        try:
            spec = get_provider(provider)
        except ValueError:
            return 0
        home = self._agent_linux_home(agent)
        if home is None or not home.exists():
            skipped.append({"agent_id": agent_id, "reason": "no linux home; workspace files not restored"})
            return 0

        workspace = home / spec.state_dir / spec.workspace_dir
        restored = 0
        for entry in sorted(workspace_backup.rglob("*"), key=str):
            if not entry.is_file():
                continue
            rel = entry.relative_to(workspace_backup)
            target = workspace / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry, target)
                if linux_user:
                    self._chown_path(target, linux_user)
                restored += 1
            except (OSError, PermissionError) as exc:
                skipped.append({"agent_id": agent_id, "reason": f"could not restore {rel}: {exc}"})
        return restored

    # ── git plumbing ─────────────────────────────────────────────────────

    @staticmethod
    def _require_backup_git() -> None:
        if not shutil.which("git"):
            raise SetupError("git is required for clawie backup. Install git and re-run.")

    @staticmethod
    def _run_backup_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=clawie",
            "-c",
            "user.email=clawie@localhost",
            "-c",
            "commit.gpgsign=false",
            *args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SetupError(f"git {' '.join(args)} failed: {detail or f'exit {result.returncode}'}")
        return result

    def _restore_backup_repo_ownership(self, repo: Path) -> None:
        """Keep the repo owned by the clawie state owner when run via sudo.

        The maintenance cron runs as root while the repo usually lives in the
        managing user's home; without this, a root-run backup would leave
        root-owned files that break the user's next manual run.
        """
        if os.geteuid() != 0 or not repo.exists():
            return
        try:
            stat = self.store.root.stat()
        except OSError:
            return
        if stat.st_uid == 0:
            return
        try:
            os.chown(repo, stat.st_uid, stat.st_gid)
        except OSError:
            return
        for child in repo.rglob("*"):
            try:
                if not child.is_symlink():
                    os.chown(child, stat.st_uid, stat.st_gid)
            except OSError:
                continue

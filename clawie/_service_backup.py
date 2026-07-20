"""Git-backed knowledge backup and restore (ClawieService mixin).

The backup repository mirrors the fleet's durable knowledge:

- ``state/snapshot.json`` — config and agent records with secrets redacted
- ``agents/<agent_id>/manifest.json`` — secret-free declarative agent manifest
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
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from clawie.manifest import AgentManifest, ChannelSpec, CredentialRef, ManifestError
from clawie.providers import get_provider
from clawie.service_common import SetupError, AgentNotFoundError, now_iso
from clawie.safe_fs import (
    ensure_directory_under,
    read_bytes_under,
    read_text_under,
    remove_under,
    write_bytes_under,
    write_text_under,
)

_BACKUP_SENTINEL = ".clawie-backup.json"
_BACKUP_FORMAT_VERSION = 1
_BACKUP_MANAGED_PATHS = (
    _BACKUP_SENTINEL,
    "README.md",
    ".gitignore",
    "state",
    "agents",
)

_BACKUP_README = """# clawie knowledge backup

This repository is maintained automatically by `clawie backup`.

Layout:

- `state/snapshot.json` — fleet config and agent records (secrets redacted)
- `agents/<agent_id>/manifest.json` — secret-free declarative agent manifest
- `agents/<agent_id>/prompts/` — core prompt files (SOUL.md, MEMORY.md, ...)
- `agents/<agent_id>/workspace/` — knowledge files captured from the agent workspace

Events are intentionally excluded so that commits only happen when knowledge
actually changes. Credentials are never written here; use `clawie backup export`
for a full-fidelity local snapshot instead.

Restore manifests, prompts, and workspace knowledge with
`clawie backup restore [--agent AGENT_ID]`.
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
_SECRET_CONTENT_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(rb"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(
        rb"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)\b"
        rb"\s*[:=]\s*[\"']?(?!<redacted>)[A-Za-z0-9._~+/=-]{12,}"
    ),
)


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
        remote_url = self._validate_backup_remote(str(remote or "").strip())
        # Anchor relative paths to the current directory now; the maintenance
        # cron runs from a different cwd and must find the same repo.
        repo = Path(os.path.abspath(str(repo)))
        if repo.exists() or repo.is_symlink():
            repo_st = repo.lstat()
            if stat.S_ISLNK(repo_st.st_mode) or not stat.S_ISDIR(repo_st.st_mode):
                raise SetupError(f"backup path must be a real directory: {repo}")
        else:
            repo.mkdir(parents=True, mode=0o700)

        sentinel = repo / _BACKUP_SENTINEL
        git_dir = repo / ".git"
        if sentinel.exists() or sentinel.is_symlink():
            self._validate_backup_repo(repo, require_git=False)
        elif any(repo.iterdir()):
            raise SetupError(
                f"refusing to adopt non-empty directory without {_BACKUP_SENTINEL}: {repo}"
            )

        created = not git_dir.exists()
        if created:
            self._run_backup_git(repo, "init", "--quiet")
        self._write_backup_sentinel(repo)
        self._validate_backup_repo(repo)

        write_text_under(repo, "README.md", _BACKUP_README, mode=0o600)
        write_text_under(repo, ".gitignore", _BACKUP_GITIGNORE, mode=0o600)

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
        self._validate_backup_repo(repo)

        result = self._write_backup_tree(repo)
        self._run_backup_git(repo, "add", "-A", "--", *_BACKUP_MANAGED_PATHS)
        dirty = bool(
            self._run_backup_git(
                repo, "status", "--porcelain", "--", *_BACKUP_MANAGED_PATHS
            ).stdout.strip()
        )

        commit = ""
        if dirty:
            commit_message = str(message).strip() or f"clawie backup {now_iso()}"
            self._run_backup_git(repo, "commit", "--quiet", "-m", commit_message)
        head = self._run_backup_git(repo, "rev-parse", "HEAD", check=False)
        if head.returncode == 0:
            commit = head.stdout.strip()

        settings = self.backup_settings()
        remote_url = self._validate_backup_remote(str(settings["remote"] or ""))
        should_push = settings["auto_push"] if push is None else bool(push)
        pushed = False
        push_error = ""
        if should_push and remote_url and commit:
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
            "initialized": False,
            "validation_error": "",
            "dirty": False,
            "head": "",
            "commit_count": 0,
        }
        if not payload["git_available"]:
            return payload
        try:
            self._validate_backup_repo(repo)
        except SetupError as exc:
            if repo.exists() or repo.is_symlink():
                payload["validation_error"] = str(exc)
            return payload
        payload["initialized"] = True
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
        self._validate_backup_repo(repo)
        agents_root = repo / "agents"
        try:
            agents_root_st = agents_root.lstat()
        except FileNotFoundError:
            agents_root_st = None
        if agents_root_st is None or stat.S_ISLNK(agents_root_st.st_mode) or not stat.S_ISDIR(
            agents_root_st.st_mode
        ):
            raise SetupError(
                f"backup repo has no agents to restore (looked at: {agents_root}). "
                "Run 'clawie backup run' first."
            )

        state = self.store.read_state()
        known_agents = state.get("agents", {})
        requested = str(agent_id or "").strip()
        if requested:
            if not _SAFE_PATH_SEGMENT.fullmatch(requested):
                raise ValueError("agent id is unsafe for backup restore paths")
            requested_root = agents_root / requested
            try:
                requested_st = requested_root.lstat()
            except FileNotFoundError:
                requested_st = None
            if requested_st is None or stat.S_ISLNK(requested_st.st_mode) or not stat.S_ISDIR(
                requested_st.st_mode
            ):
                raise AgentNotFoundError(f"agent not found in backup repo: {requested}")
            targets = [requested]
        else:
            targets = []
            for entry in sorted(agents_root.iterdir(), key=lambda item: item.name):
                if not _SAFE_PATH_SEGMENT.fullmatch(entry.name):
                    continue
                entry_st = entry.lstat()
                if not stat.S_ISLNK(entry_st.st_mode) and stat.S_ISDIR(entry_st.st_mode):
                    targets.append(entry.name)

        restored: dict[str, dict[str, int]] = {}
        skipped: list[dict[str, str]] = []
        for token in targets:
            agent_root = agents_root / token
            if token not in known_agents:
                created, reason = self._restore_agent_from_backup_manifest(token, agent_root)
                if not created:
                    if requested:
                        if reason == "not in local state and backup has no manifest":
                            raise AgentNotFoundError(
                                f"agent '{token}' exists in the backup but not in local state and "
                                "the backup has no manifest to recreate it"
                            )
                        raise SetupError(f"could not restore agent '{token}' from backup manifest: {reason}")
                    skipped.append({"agent_id": token, "reason": reason})
                    continue
                if reason:
                    skipped.append({"agent_id": token, "reason": reason})
                state = self.store.read_state()
                known_agents = state.get("agents", {})
                if token not in known_agents:
                    if requested:
                        raise SetupError(
                            f"agent '{token}' exists in the backup but not in local state and "
                            "manifest reconcile did not create it"
                        )
                    skipped.append({"agent_id": token, "reason": "manifest reconcile did not create agent"})
                    continue

            prompts_restored = self._restore_agent_prompts(token, agent_root / "prompts")
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
                    token, agent_root / "workspace", skipped
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

        self._validate_backup_repo(repo)
        for stale in ("state", "agents"):
            remove_under(repo, stale, recursive=True)
        state_dir = ensure_directory_under(repo, "state", mode=0o700)
        agents_dir = ensure_directory_under(repo, "agents", mode=0o700)

        snapshot = {
            "config": self._redacted_backup_config(),
            "state": self._redacted_backup_state(),
        }
        snapshot_bytes = (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if self._contains_secret_material(snapshot_bytes):
            raise SetupError("refusing to write backup snapshot because secret-like content remains")
        write_bytes_under(state_dir, "snapshot.json", snapshot_bytes, mode=0o600)
        files_written += 1

        state = self.store.read_state()
        agents = state.get("agents", {})
        for token, agent in sorted(agents.items()):
            if not isinstance(agent, dict):
                continue
            safe_id = str(token).strip()
            if not _SAFE_PATH_SEGMENT.fullmatch(safe_id):
                skipped.append({"agent_id": safe_id, "reason": "unsafe agent id for backup paths"})
                continue
            agent_root = agents_dir / safe_id
            provider = str(agent.get("agent", {}).get("provider", "")).strip().lower()
            files_written += self._write_agent_manifest_backup(agent_root, safe_id, agent)
            files_written += self._write_agent_prompt_backups(
                agent_root, provider, agent, skipped, agent_id=safe_id
            )
            files_written += self._write_agent_workspace_backups(
                agent_root, provider, agent, skipped, agent_id=safe_id
            )
            agents_backed_up.append(safe_id)
        return {"agents": agents_backed_up, "files": files_written, "skipped": skipped}

    def _write_agent_manifest_backup(self, agent_root: Path, agent_id: str, agent: dict[str, Any]) -> int:
        manifest = self._agent_manifest_from_state(agent_id, agent)
        ensure_directory_under(agent_root.parent, agent_root.name, mode=0o700)
        write_text_under(agent_root, "manifest.json", manifest.to_json(), mode=0o600)
        return 1

    def _agent_manifest_from_state(self, agent_id: str, agent: dict[str, Any]) -> AgentManifest:
        info = agent.get("agent", {}) if isinstance(agent.get("agent", {}), dict) else {}
        role = str(info.get("role", "worker")).strip().lower()
        if role not in {"worker", "control"}:
            role = "worker"
        model_tier = str(info.get("model_tier", "balanced")).strip().lower()
        if model_tier not in {"fast", "balanced", "power"}:
            model_tier = "balanced"

        channels: list[ChannelSpec] = []
        for row in agent.get("channels", []):
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind", "")).strip().lower()
            name = str(row.get("name", "")).strip()
            if not kind or not name:
                continue
            if self._is_sensitive_manifest_channel(kind, name):
                continue
            allow_from = tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in row.get("allow_from", [])
                    if str(item).strip()
                )
            )
            channels.append(ChannelSpec(kind=kind, name=name, allow_from=allow_from))

        sync = self._normalize_credential_sync_state(
            agent.get("credential_sync"),
            default_when_missing=True,
        )
        credential_scopes = sync.get("credential_scopes", {})
        credentials = [
            CredentialRef(
                name=str(bundle),
                scope=str(credential_scopes.get(str(bundle), "agent")),
            )
            for bundle in sync.get("bundles", [])
            if str(bundle).strip()
        ]

        addons = {
            name: True
            for name, data in self._normalize_agent_addons(agent.get("addons")).items()
            if bool(data.get("enabled", False))
        }

        display_name = str(agent.get("display_name", agent_id)).strip() or agent_id
        if self._contains_secret_material(display_name.encode("utf-8")):
            display_name = agent_id
        return AgentManifest(
            id=agent_id,
            provider=str(info.get("provider", "openclaw")).strip().lower() or "openclaw",
            role=role,
            model_tier=model_tier,
            display_name=display_name,
            prompts_dir=str(agent.get("manifest_prompts_dir", "prompts")).strip() or "prompts",
            channels=channels,
            credentials=credentials,
            addons=addons,
            limits=dict(info.get("limits", {})) if isinstance(info.get("limits"), dict) else {},
        )

    def _is_sensitive_manifest_channel(self, kind: str, name: str) -> bool:
        if self._contains_secret_material(name.encode("utf-8")):
            return True
        if self._looks_like_unresolved_secret(name):
            return True
        if kind == "telegram" and self._looks_like_telegram_bot_token(name):
            return True
        return False

    def _write_agent_prompt_backups(
        self,
        agent_root: Path,
        provider: str,
        agent: dict[str, Any],
        skipped: list[dict[str, str]],
        *,
        agent_id: str,
    ) -> int:
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
            encoded = body.encode("utf-8")
            if self._contains_secret_material(encoded):
                skipped.append(
                    {"agent_id": agent_id, "reason": f"prompt {token} contains secret-like content"}
                )
                continue
            write_bytes_under(agent_root, Path("prompts") / token, encoded, mode=0o600)
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
            try:
                data = read_bytes_under(
                    home,
                    Path(spec.state_dir) / spec.workspace_dir / rel,
                    max_bytes=_MAX_KNOWLEDGE_FILE_BYTES,
                )
                if self._contains_secret_material(data):
                    skipped.append(
                        {"agent_id": agent_id, "reason": f"workspace file {rel} contains secret-like content"}
                    )
                    continue
                write_bytes_under(
                    agent_root,
                    Path("workspace") / rel,
                    data,
                    mode=0o600,
                )
                written += 1
            except (OSError, PermissionError, ValueError) as exc:
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
        source = self.store.read_config()
        safe_keys = (
            "schema_version",
            "provider",
            "auth_mode",
            "subscription",
            "workspace",
            "runtime_installed",
            "maintenance_cron_enabled",
            "maintenance_cron_interval_hours",
            "backup_enabled",
            "backup_auto_push",
            "created_at",
            "updated_at",
        )
        config = {key: copy.deepcopy(source.get(key)) for key in safe_keys}
        if str(source.get("api_key", "") or "").strip():
            config["api_key"] = "<redacted>"
        if str(source.get("spawn_password_hash", "") or "").strip():
            config["spawn_password_hash"] = "<redacted>"
        api_url = self._sanitize_backup_url(str(source.get("api_url", "") or ""))
        if api_url:
            config["api_url"] = api_url
        provider_credentials = source.get("provider_credentials", {})
        if isinstance(provider_credentials, dict):
            config["provider_credentials"] = {
                str(provider): {
                    "api_url": self._sanitize_backup_url(str(payload.get("api_url", "") or "")),
                    "api_key": "<redacted>" if str(payload.get("api_key", "") or "").strip() else "",
                }
                for provider, payload in provider_credentials.items()
                if isinstance(payload, dict)
            }
        return config

    def _redacted_backup_state(self) -> dict[str, Any]:
        source = self.store.read_state()
        safe: dict[str, Any] = {"agents": {}, "templates": {}}
        agents = source.get("agents", {})
        if not isinstance(agents, dict):
            return safe
        for agent_id, payload in agents.items():
            if not isinstance(payload, dict):
                continue
            info = payload.get("agent", {}) if isinstance(payload.get("agent"), dict) else {}
            safe_info = {
                key: copy.deepcopy(info.get(key))
                for key in (
                    "provider",
                    "role",
                    "model_tier",
                    "agent_version",
                    "runtime",
                    "autostart",
                    "heartbeat_seconds",
                    "gateway_port",
                    "linux_user",
                    "ssh_login_disabled",
                )
                if key in info
            }
            if str(info.get("gateway_token", "") or "").strip():
                safe_info["gateway_token"] = "<redacted>"
            safe["agents"][str(agent_id)] = {
                "agent_id": str(payload.get("agent_id", agent_id)),
                "display_name": str(payload.get("display_name", agent_id)),
                "agent": safe_info,
                "channels": [
                    {"kind": str(row.get("kind", "")), "name": self._safe_backup_channel_name(row)}
                    for row in payload.get("channels", [])
                    if isinstance(row, dict)
                ],
                "addons": {
                    str(name): {"enabled": bool(data.get("enabled", False))}
                    for name, data in payload.get("addons", {}).items()
                    if isinstance(data, dict)
                }
                if isinstance(payload.get("addons"), dict)
                else {},
            }
        return safe

    @staticmethod
    def _contains_secret_material(data: bytes) -> bool:
        return any(pattern.search(data) is not None for pattern in _SECRET_CONTENT_PATTERNS)

    def _safe_backup_channel_name(self, row: dict[str, Any]) -> str:
        name = str(row.get("name", "") or "")
        kind = str(row.get("kind", "") or "").strip().lower()
        if self._is_sensitive_manifest_channel(kind, name):
            return "<redacted>"
        return name

    @staticmethod
    def _sanitize_backup_url(value: str) -> str:
        token = str(value or "").strip()
        if not token:
            return ""
        try:
            parsed = urlsplit(token)
        except ValueError:
            return ""
        if not parsed.scheme or not parsed.netloc:
            return ""
        host = parsed.hostname or ""
        if not host:
            return ""
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            return ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))

    @staticmethod
    def _validate_backup_remote(value: str) -> str:
        token = str(value or "").strip()
        if not token:
            return ""
        try:
            parsed = urlsplit(token)
        except ValueError as exc:
            raise SetupError(f"invalid backup remote URL: {exc}") from exc
        if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
            raise SetupError("backup remote URL must not contain embedded credentials")
        sensitive_query_keys = {
            "access_token",
            "api_key",
            "apikey",
            "auth",
            "key",
            "password",
            "secret",
            "sig",
            "signature",
            "token",
        }
        if any(key.strip().lower() in sensitive_query_keys for key, _ in parse_qsl(parsed.query)):
            raise SetupError("backup remote URL must not contain credential query parameters")
        if parsed.fragment:
            raise SetupError("backup remote URL must not contain a fragment")
        return token

    def _write_backup_sentinel(self, repo: Path) -> None:
        sentinel = repo / _BACKUP_SENTINEL
        repository_id = ""
        if sentinel.exists() and not sentinel.is_symlink():
            try:
                current = json.loads(read_text_under(repo, _BACKUP_SENTINEL, max_bytes=16 * 1024))
            except (OSError, ValueError, json.JSONDecodeError):
                current = {}
            if isinstance(current, dict):
                repository_id = str(current.get("repository_id", "") or "").strip()
        payload = {
            "format_version": _BACKUP_FORMAT_VERSION,
            "managed_by": "clawie",
            "repository_id": repository_id or uuid.uuid4().hex,
        }
        write_text_under(
            repo,
            _BACKUP_SENTINEL,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )

    def _validate_backup_repo(self, repo: Path, *, require_git: bool = True) -> None:
        try:
            repo_st = repo.lstat()
        except FileNotFoundError as exc:
            raise SetupError(f"backup repository does not exist: {repo}") from exc
        if stat.S_ISLNK(repo_st.st_mode) or not stat.S_ISDIR(repo_st.st_mode):
            raise SetupError(f"backup repository must be a real directory: {repo}")
        sentinel = repo / _BACKUP_SENTINEL
        try:
            sentinel_st = sentinel.lstat()
        except FileNotFoundError as exc:
            raise SetupError(
                f"refusing unowned backup repository without {_BACKUP_SENTINEL}: {repo}"
            ) from exc
        if stat.S_ISLNK(sentinel_st.st_mode) or not stat.S_ISREG(sentinel_st.st_mode):
            raise SetupError(f"backup repository sentinel is not a regular file: {sentinel}")
        try:
            payload = json.loads(read_text_under(repo, _BACKUP_SENTINEL, max_bytes=16 * 1024))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SetupError(f"invalid backup repository sentinel: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("managed_by") != "clawie":
            raise SetupError("backup repository sentinel is not owned by clawie")
        if payload.get("format_version") != _BACKUP_FORMAT_VERSION:
            raise SetupError("unsupported backup repository format version")
        if not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("repository_id", "") or "")):
            raise SetupError("backup repository sentinel has an invalid repository id")
        if not require_git:
            return
        git_dir = repo / ".git"
        try:
            git_st = git_dir.lstat()
        except FileNotFoundError as exc:
            raise SetupError(f"backup repository has no .git directory: {repo}") from exc
        if stat.S_ISLNK(git_st.st_mode) or not stat.S_ISDIR(git_st.st_mode):
            raise SetupError(f"backup .git path must be a real directory: {git_dir}")

    # ── restore helpers ──────────────────────────────────────────────────

    def _restore_agent_from_backup_manifest(self, agent_id: str, agent_root: Path) -> tuple[bool, str]:
        manifest_path = agent_root / "manifest.json"
        try:
            manifest_st = manifest_path.lstat()
        except FileNotFoundError:
            return False, "not in local state and backup has no manifest"
        if stat.S_ISLNK(manifest_st.st_mode) or not stat.S_ISREG(manifest_st.st_mode):
            return False, "backup manifest is not a regular file"
        try:
            manifest = AgentManifest.from_json(
                read_text_under(agent_root, "manifest.json", max_bytes=_MAX_KNOWLEDGE_FILE_BYTES)
            )
        except (OSError, ManifestError, ValueError) as exc:
            return False, f"invalid manifest: {exc}"
        if manifest.id != agent_id:
            return False, f"manifest id {manifest.id!r} does not match backup path {agent_id!r}"

        self.write_agent_manifest(manifest)
        result = self.reconcile_agent_manifest(manifest, dry_run=False)
        errors = result.get("errors", [])
        if not errors:
            return True, ""

        try:
            self.get_agent(agent_id)
        except AgentNotFoundError:
            detail = "; ".join(str(row.get("error", "")) for row in errors if isinstance(row, dict))
            return False, f"manifest reconcile failed: {detail or errors!r}"

        detail = "; ".join(str(row.get("error", "")) for row in errors if isinstance(row, dict))
        return True, f"manifest reconcile incomplete: {detail or errors!r}"

    def _restore_agent_prompts(self, agent_id: str, prompts_dir: Path) -> int:
        try:
            root_st = prompts_dir.lstat()
        except FileNotFoundError:
            return 0
        if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
            raise SetupError(f"backup prompts path is not a real directory: {prompts_dir}")
        payload = self.get_dashboard_agent(agent_id)
        provider = str(payload.get("agent", {}).get("provider", "")).strip().lower()
        valid_names = set(self._provider_core_prompt_names(provider))
        restored = 0
        for entry in sorted(prompts_dir.iterdir()):
            entry_st = entry.lstat()
            if stat.S_ISLNK(entry_st.st_mode):
                raise SetupError(f"backup prompt is a symlink: {entry}")
            if not stat.S_ISREG(entry_st.st_mode) or entry.name not in valid_names:
                continue
            content = read_text_under(prompts_dir, entry.name, max_bytes=_MAX_KNOWLEDGE_FILE_BYTES)
            self.set_agent_core_prompt(agent_id, entry.name, content, sync_to_disk=False)
            restored += 1
        return restored

    def _restore_agent_workspace(
        self,
        agent_id: str,
        workspace_backup: Path,
        skipped: list[dict[str, str]],
    ) -> int:
        try:
            backup_st = workspace_backup.lstat()
        except FileNotFoundError:
            return 0
        if stat.S_ISLNK(backup_st.st_mode) or not stat.S_ISDIR(backup_st.st_mode):
            raise SetupError(f"backup workspace is not a real directory: {workspace_backup}")
        state = self.store.read_state()
        agent = state.get("agents", {}).get(agent_id, {})
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

        restored = 0
        for entry in sorted(workspace_backup.rglob("*"), key=str):
            entry_st = entry.lstat()
            if stat.S_ISLNK(entry_st.st_mode):
                skipped.append({"agent_id": agent_id, "reason": f"refused backup symlink: {entry}"})
                continue
            if stat.S_ISDIR(entry_st.st_mode):
                continue
            if not stat.S_ISREG(entry_st.st_mode):
                skipped.append({"agent_id": agent_id, "reason": f"refused special backup file: {entry}"})
                continue
            rel = entry.relative_to(workspace_backup)
            try:
                data = read_bytes_under(
                    workspace_backup,
                    rel,
                    max_bytes=_MAX_KNOWLEDGE_FILE_BYTES,
                )
                write_bytes_under(
                    home,
                    Path(spec.state_dir) / spec.workspace_dir / rel,
                    data,
                    mode=0o600,
                    directory_mode=0o700,
                    owner=self._agent_owner(linux_user),
                )
                restored += 1
            except (OSError, ValueError) as exc:
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

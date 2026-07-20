"""Control-agent outward escalation helpers."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from clawie.service_common import SetupError, now_iso


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent bearer credentials from being forwarded through redirects."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class ControlEscalationMixin:
    """GitHub escalation for confirmed control-agent outward actions."""

    def configure_control_escalation(
        self,
        *,
        github_repo: str | None = None,
        github_token_path: str | None = None,
        operators: list[str] | None = None,
        issue_labels: list[str] | None = None,
        rate_limit_seconds: int | None = None,
    ) -> dict[str, Any]:
        config = self.store.read_config()
        if github_repo is not None:
            config["control_github_repo"] = self._normalize_github_repo(github_repo)
        if github_token_path is not None:
            config["control_github_token_path"] = str(Path(github_token_path).expanduser())
        if operators is not None:
            config["control_operator_allowlist"] = self._normalized_control_list(operators)
        if issue_labels is not None:
            config["control_github_issue_labels"] = self._normalized_control_list(issue_labels)
        if rate_limit_seconds is not None:
            config["control_github_rate_limit_seconds"] = max(0, int(rate_limit_seconds))
        self.store.write_config(config)
        return self.control_escalation_settings()

    def control_escalation_settings(self) -> dict[str, Any]:
        config = self.store.read_config()
        token_path = str(config.get("control_github_token_path", "")).strip()
        return {
            "github_repo": str(config.get("control_github_repo", "")).strip(),
            "github_token_path": token_path,
            "github_token_configured": bool(token_path),
            "operator_allowlist": self._normalized_control_list(
                config.get("control_operator_allowlist", [])
            ),
            "issue_labels": self._normalized_control_list(
                config.get("control_github_issue_labels", [])
            ),
            "rate_limit_seconds": int(config.get("control_github_rate_limit_seconds", 0) or 0),
        }

    def open_control_issue(
        self,
        *,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required")
        body = str(body or "")
        config = self.store.read_config()
        repo = self._normalize_github_repo(str(config.get("control_github_repo", "")))
        if not repo:
            raise SetupError("control GitHub repo is not configured")
        token_path = str(config.get("control_github_token_path", "")).strip()
        token = self._read_control_github_token(token_path)
        issue_labels = self._normalized_control_list(
            labels if labels is not None else config.get("control_github_issue_labels", [])
        )
        key = str(dedupe_key or "").strip() or self._default_issue_dedupe_key(title, body)

        local_state = config.setdefault("local_service_state", {})
        if not isinstance(local_state, dict):
            local_state = {}
            config["local_service_state"] = local_state
        escalation_state = local_state.setdefault("control_escalation", {})
        if not isinstance(escalation_state, dict):
            escalation_state = {}
            local_state["control_escalation"] = escalation_state
        issues = escalation_state.setdefault("issues", {})
        if not isinstance(issues, dict):
            issues = {}
            escalation_state["issues"] = issues

        duplicate = issues.get(key)
        if isinstance(duplicate, dict):
            return {
                "created": False,
                "duplicate": True,
                "rate_limited": False,
                "repo": repo,
                "dedupe_key": key,
                "number": int(duplicate.get("number", 0) or 0),
                "url": str(duplicate.get("url", "")),
                "title": str(duplicate.get("title", title)),
            }

        now_epoch = time.time()
        rate_limit_seconds = int(config.get("control_github_rate_limit_seconds", 0) or 0)
        last_epoch = float(escalation_state.get("last_issue_opened_at_epoch", 0.0) or 0.0)
        if rate_limit_seconds > 0 and last_epoch > 0 and now_epoch - last_epoch < rate_limit_seconds:
            return {
                "created": False,
                "duplicate": False,
                "rate_limited": True,
                "retry_after_seconds": int(rate_limit_seconds - (now_epoch - last_epoch)),
                "repo": repo,
                "dedupe_key": key,
                "title": title,
            }

        response = self._github_json_request(
            "POST",
            f"https://api.github.com/repos/{repo}/issues",
            token=token,
            payload={"title": title, "body": body, "labels": issue_labels},
        )
        number = int(response.get("number", 0) or 0)
        url = str(response.get("html_url", ""))
        row = {
            "number": number,
            "url": url,
            "title": title,
            "opened_at": now_iso(),
            "dedupe_key": key,
        }
        issues[key] = row
        escalation_state["last_issue_opened_at_epoch"] = now_epoch
        self.store.write_config(config)

        state = self.store.read_state()
        self._event(
            state,
            "control.github_issue_opened",
            f"Opened control GitHub issue {repo}#{number}",
            {"repo": repo, "number": number, "url": url, "dedupe_key": key, "labels": issue_labels},
        )
        self.store.write_state(state)
        return {
            "created": True,
            "duplicate": False,
            "rate_limited": False,
            "repo": repo,
            "dedupe_key": key,
            "number": number,
            "url": url,
            "title": title,
        }

    def open_control_pr(
        self,
        *,
        title: str,
        head: str,
        base: str = "main",
        body: str = "",
        draft: bool = True,
        maintainer_can_modify: bool = False,
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        title = str(title or "").strip()
        head = str(head or "").strip()
        base = str(base or "").strip() or "main"
        if not title:
            raise ValueError("title is required")
        if not head:
            raise ValueError("head branch is required")
        body = str(body or "")
        config = self.store.read_config()
        repo = self._normalize_github_repo(str(config.get("control_github_repo", "")))
        if not repo:
            raise SetupError("control GitHub repo is not configured")
        token = self._read_control_github_token(str(config.get("control_github_token_path", "")).strip())
        key = str(dedupe_key or "").strip() or self._default_issue_dedupe_key(
            f"{title}:{head}:{base}",
            body,
        )

        escalation_state = self._control_escalation_state(config)
        pull_requests = escalation_state.setdefault("pull_requests", {})
        if not isinstance(pull_requests, dict):
            pull_requests = {}
            escalation_state["pull_requests"] = pull_requests

        duplicate = pull_requests.get(key)
        if isinstance(duplicate, dict):
            return {
                "created": False,
                "duplicate": True,
                "rate_limited": False,
                "repo": repo,
                "dedupe_key": key,
                "number": int(duplicate.get("number", 0) or 0),
                "url": str(duplicate.get("url", "")),
                "title": str(duplicate.get("title", title)),
                "head": str(duplicate.get("head", head)),
                "base": str(duplicate.get("base", base)),
            }

        now_epoch = time.time()
        rate_limit_seconds = int(config.get("control_github_rate_limit_seconds", 0) or 0)
        last_epoch = float(escalation_state.get("last_pr_opened_at_epoch", 0.0) or 0.0)
        if rate_limit_seconds > 0 and last_epoch > 0 and now_epoch - last_epoch < rate_limit_seconds:
            return {
                "created": False,
                "duplicate": False,
                "rate_limited": True,
                "retry_after_seconds": int(rate_limit_seconds - (now_epoch - last_epoch)),
                "repo": repo,
                "dedupe_key": key,
                "title": title,
                "head": head,
                "base": base,
            }

        response = self._github_json_request(
            "POST",
            f"https://api.github.com/repos/{repo}/pulls",
            token=token,
            payload={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": bool(draft),
                "maintainer_can_modify": bool(maintainer_can_modify),
            },
        )
        number = int(response.get("number", 0) or 0)
        url = str(response.get("html_url", ""))
        row = {
            "number": number,
            "url": url,
            "title": title,
            "head": head,
            "base": base,
            "opened_at": now_iso(),
            "dedupe_key": key,
            "draft": bool(draft),
        }
        pull_requests[key] = row
        escalation_state["last_pr_opened_at_epoch"] = now_epoch
        self.store.write_config(config)

        state = self.store.read_state()
        self._event(
            state,
            "control.github_pr_opened",
            f"Opened control GitHub PR {repo}#{number}",
            {
                "repo": repo,
                "number": number,
                "url": url,
                "dedupe_key": key,
                "head": head,
                "base": base,
                "draft": bool(draft),
            },
        )
        self.store.write_state(state)
        return {
            "created": True,
            "duplicate": False,
            "rate_limited": False,
            "repo": repo,
            "dedupe_key": key,
            "number": number,
            "url": url,
            "title": title,
            "head": head,
            "base": base,
            "draft": bool(draft),
        }

    def _control_escalation_state(self, config: dict[str, Any]) -> dict[str, Any]:
        local_state = config.setdefault("local_service_state", {})
        if not isinstance(local_state, dict):
            local_state = {}
            config["local_service_state"] = local_state
        escalation_state = local_state.setdefault("control_escalation", {})
        if not isinstance(escalation_state, dict):
            escalation_state = {}
            local_state["control_escalation"] = escalation_state
        return escalation_state

    @staticmethod
    def _default_issue_dedupe_key(title: str, body: str) -> str:
        digest = hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()
        return digest[:24]

    @staticmethod
    def _normalize_github_repo(repo: str) -> str:
        token = str(repo or "").strip()
        if token.startswith("https://github.com/"):
            token = token.removeprefix("https://github.com/").strip("/")
        parts = [part for part in token.split("/") if part]
        if len(parts) != 2:
            return ""
        owner, name = parts
        return f"{owner}/{name}"

    @staticmethod
    def _normalized_control_list(raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [item.strip() for item in raw.split(",")]
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            token = str(item or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            result.append(token)
        return result

    @staticmethod
    def _read_control_github_token(token_path: str) -> str:
        path = Path(str(token_path or "")).expanduser()
        if not str(token_path or "").strip():
            raise SetupError("control GitHub token path is not configured")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(path, flags)
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise SetupError("control GitHub token path must be a regular file")
            if file_stat.st_mode & 0o077:
                raise SetupError("control GitHub token file must not be group/world accessible")
            if file_stat.st_size > 64 * 1024:
                raise SetupError("control GitHub token file exceeds 64 KiB")
            chunks: list[bytes] = []
            remaining = 64 * 1024 + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 8192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            token = b"".join(chunks).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise SetupError(f"control GitHub token file is not readable: {path}") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if not token:
            raise SetupError("control GitHub token file is empty")
        return token

    @staticmethod
    def _github_json_request(
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        parsed_url = urlsplit(str(url))
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "api.github.com"
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise SetupError("control GitHub requests must target https://api.github.com")
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=str(method).upper(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "clawie-control",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        opener = urllib.request.build_opener(_RejectRedirectHandler())
        try:
            with opener.open(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SetupError(f"GitHub API request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SetupError(f"GitHub API request failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SetupError(f"GitHub API request returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SetupError("GitHub API request returned a non-object response")
        return parsed

"""Channel discovery, assignment, sync, and connect flows (ClawieService mixin)."""
from __future__ import annotations

import copy
import fcntl
import hmac
import http.client
import json
import math
import os
import re
import subprocess
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]  # Python 3.10 fallback
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]
from clawie.provider_channels import (
    dedupe_channels,
    get_channel_adapter,
    is_openclaw_channel_placeholder,
)
from clawie.providers import (
    get_provider,
    provider_names,
)
from clawie.service_common import SetupError, AgentNotFoundError, now_iso


_OPENCLAW_QR_CHANNELS: dict[str, dict[str, str]] = {
    "wechat": {
        "canonical": "openclaw-weixin",
        "label": "WeChat",
        "plugin_id": "openclaw-weixin",
        "package": "@tencent-weixin/openclaw-weixin",
    },
    "openclaw-weixin": {
        "canonical": "openclaw-weixin",
        "label": "WeChat",
        "plugin_id": "openclaw-weixin",
        "package": "@tencent-weixin/openclaw-weixin",
    },
    "weixin": {
        "canonical": "openclaw-weixin",
        "label": "WeChat",
        "plugin_id": "openclaw-weixin",
        "package": "@tencent-weixin/openclaw-weixin",
    },
    "whatsapp": {
        "canonical": "whatsapp",
        "label": "WhatsApp",
        "plugin_id": "whatsapp",
        "package": "@openclaw/whatsapp",
    },
}


class ChannelOpsMixin:

    @staticmethod
    @contextmanager
    def _openclaw_telegram_setup_lock(
        home: Path,
        agent_id: str,
        label: str = "Telegram",
    ) -> Iterator[None]:
        """Serialize setup without creating lock files in the managed home."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(home, flags)
        except OSError as exc:
            raise SetupError(
                f"could not safely lock the managed home for '{agent_id}'; "
                "no files, services, or agent state were changed"
            ) from exc
        locked = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                raise SetupError(
                    f"{label} setup is already running for '{agent_id}'; wait for it to finish, "
                    "then run the status command (no files, services, or agent state were changed)"
                ) from None
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _openclaw_telegram_agent_context(
        self,
        agent_id: str,
        *,
        purpose: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, Path]:
        """Resolve a managed OpenClaw agent and enforce its OS boundary."""
        self._require_setup()
        target = str(agent_id).strip()
        if not target:
            raise ValueError("agent_id is required")
        if target.startswith("@local:"):
            raise ValueError("Telegram management is only supported for managed agents")
        state = self.store.read_state()
        agent = state.setdefault("agents", {}).get(target)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {target}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if provider != "openclaw":
            raise SetupError(
                f"Telegram onboarding currently requires an OpenClaw agent; "
                f"'{target}' uses {provider or 'no provider'}"
            )
        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            raise SetupError(
                f"agent '{target}' has no managed Linux user; create it with "
                f"'sudo clawie runtime create {target}' first"
            )
        self._require_linux_user_access(linux_user, purpose)
        home = self._agent_linux_home(agent)
        if home is None:
            raise SetupError(f"could not resolve the managed home for agent '{target}'")
        return agent, info, linux_user, home

    def _openclaw_qr_agent_context(
        self,
        agent_id: str,
        *,
        purpose: str,
        label: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, Path]:
        self._require_setup()
        target = str(agent_id).strip()
        if not target:
            raise ValueError("agent_id is required")
        if target.startswith("@local:"):
            raise ValueError(f"{label} management is only supported for managed agents")
        state = self.store.read_state()
        agent = state.setdefault("agents", {}).get(target)
        if not isinstance(agent, dict):
            raise AgentNotFoundError(f"agent not found: {target}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if provider != "openclaw":
            raise SetupError(
                f"{label} onboarding requires an OpenClaw agent; "
                f"'{target}' uses {provider or 'no provider'}"
            )
        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            raise SetupError(
                f"agent '{target}' has no managed Linux user; create it with "
                f"'sudo clawie runtime create {target}' first"
            )
        self._require_linux_user_access(linux_user, purpose)
        home = self._agent_linux_home(agent)
        if home is None:
            raise SetupError(f"could not resolve the managed home for agent '{target}'")
        return agent, info, linux_user, home

    @staticmethod
    def _openclaw_qr_channel_spec(channel: str) -> dict[str, str]:
        token = str(channel).strip().lower()
        spec = _OPENCLAW_QR_CHANNELS.get(token)
        if spec is None:
            raise ValueError("QR channel must be one of: wechat, whatsapp")
        return dict(spec)

    @staticmethod
    def _probe_telegram_bot_token(token: str, *, timeout: float = 10.0) -> dict[str, str]:
        """Verify a bot token without allowing provider errors to disclose it."""
        connection = http.client.HTTPSConnection(
            "api.telegram.org",
            timeout=max(1.0, float(timeout)),
        )
        try:
            connection.request(
                "POST",
                f"/bot{token}/getMe",
                body=b"",
                headers={"Content-Length": "0"},
            )
            response = connection.getresponse()
            body = response.read(65_537)
            if response.status != 200 or len(body) > 65_536:
                raise SetupError(
                    "Telegram rejected the bot token; no files, services, or agent state were changed"
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise SetupError(
                    "Telegram returned an unreadable token-validation response; "
                    "no files, services, or agent state were changed"
                ) from None
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            if (
                not isinstance(payload, dict)
                or payload.get("ok") is not True
                or not isinstance(result, dict)
                or result.get("is_bot") is not True
            ):
                raise SetupError(
                    "Telegram rejected the bot token; no files, services, or agent state were changed"
                )
            return {
                "bot_id": str(result.get("id", "")).strip(),
                "bot_username": str(result.get("username", "")).strip()[:80],
            }
        except SetupError:
            raise
        except Exception:
            # Transport exceptions may embed the request path in their text.
            # Never chain or surface them because that path contains the token.
            raise SetupError(
                "Telegram token validation could not reach the Bot API; "
                "no files, services, or agent state were changed"
            ) from None
        finally:
            connection.close()

    @staticmethod
    def _parse_existing_openclaw_config(content: str, *, exists: bool) -> dict[str, Any]:
        if not exists:
            return {}
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            raise SetupError(
                "the existing OpenClaw configuration is invalid JSON; repair it before Telegram setup "
                "(no files, services, or agent state were changed)"
            ) from None
        if not isinstance(payload, dict):
            raise SetupError(
                "the existing OpenClaw configuration must be a JSON object; repair it before "
                "Telegram setup (no files, services, or agent state were changed)"
            )
        return payload

    def _telegram_channel_name_for_agent(
        self,
        state: dict[str, Any],
        agent_id: str,
    ) -> str:
        agents = state.get("agents", {})
        target = agents.get(agent_id, {}) if isinstance(agents, dict) else {}
        rows = target.get("channels", []) if isinstance(target, dict) else []
        names = {
            str(row.get("name", "")).strip()
            for row in rows
            if isinstance(row, dict)
            and str(row.get("kind", "")).strip().lower() == "telegram"
            and str(row.get("name", "")).strip()
        }
        if len(names) > 1:
            raise SetupError(
                f"agent '{agent_id}' has multiple Telegram channel assignments; remove the stale "
                "assignment before setup (no files, services, or agent state were changed)"
            )
        channel_name = next(iter(names), f"{agent_id}-telegram")
        if isinstance(agents, dict):
            self._assert_channels_unclaimed(
                agents,
                agent_id,
                [{"kind": "telegram", "name": channel_name}],
            )
        return channel_name

    @staticmethod
    def _existing_telegram_token(
        config: dict[str, Any],
        token_file_content: str,
        *,
        token_file_exists: bool,
    ) -> tuple[bool, str]:
        channels = config.get("channels", {})
        telegram = channels.get("telegram", {}) if isinstance(channels, dict) else {}
        telegram = telegram if isinstance(telegram, dict) else {}
        configured = bool(
            telegram.get("enabled", False)
            or telegram.get("tokenFile")
            or telegram.get("token_file")
            or telegram.get("botToken")
        )
        if token_file_exists:
            return True, token_file_content.strip()
        inline = str(telegram.get("botToken", "")).strip()
        return configured, inline

    def _write_scoped_openclaw_telegram_config(
        self,
        *,
        home: Path,
        linux_user: str,
        config: dict[str, Any],
        token_path: Path,
    ) -> tuple[Path, str]:
        next_config = copy.deepcopy(config)
        channels = next_config.get("channels", {})
        if not isinstance(channels, dict):
            channels = {}
        telegram = channels.get("telegram", {})
        if not isinstance(telegram, dict):
            telegram = {}
        self._set_openclaw_telegram_streaming_off(telegram)
        telegram["enabled"] = True
        telegram["tokenFile"] = str(token_path)
        telegram.pop("token_file", None)
        telegram.pop("botToken", None)
        telegram.pop("bot_token", None)
        telegram.pop("token", None)
        allow_from = self._coerce_string_list(telegram.get("allowFrom", []))
        if allow_from:
            telegram["allowFrom"] = allow_from
            telegram["dmPolicy"] = "open" if "*" in set(allow_from) else "allowlist"
        else:
            telegram.pop("allowFrom", None)
            telegram["dmPolicy"] = "pairing"
        channels["telegram"] = telegram
        next_config["channels"] = channels
        return (
            self._write_agent_json_file(
                home,
                ".openclaw/openclaw.json",
                next_config,
                linux_user,
            ),
            str(telegram["dmPolicy"]),
        )

    def _restore_telegram_files(
        self,
        *,
        home: Path,
        linux_user: str,
        token_existed: bool,
        token_content: str,
        config_existed: bool,
        config_content: str,
    ) -> None:
        if token_existed:
            self._write_agent_text_file(
                home,
                ".openclaw/telegram.token",
                token_content,
                linux_user,
                mode=0o600,
            )
        else:
            self._remove_agent_path(home, ".openclaw/telegram.token")
        if config_existed:
            self._write_agent_text_file(
                home,
                ".openclaw/openclaw.json",
                config_content,
                linux_user,
                mode=0o600,
            )
        else:
            self._remove_agent_path(home, ".openclaw/openclaw.json")

    @staticmethod
    def _parse_openclaw_json_output(output: str, *, purpose: str) -> dict[str, Any]:
        """Parse OpenClaw JSON even when a version warning precedes it."""
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(output or "")).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise SetupError(
                    f"OpenClaw returned an unreadable response while {purpose}; "
                    "verify the pinned runtime with 'clawie runtime version'"
                ) from None
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise SetupError(
                    f"OpenClaw returned invalid JSON while {purpose}; "
                    "verify the pinned runtime with 'clawie runtime version'"
                ) from exc
        if not isinstance(payload, dict):
            raise SetupError(f"OpenClaw returned an invalid response while {purpose}")
        return payload

    def _run_openclaw_agent_command(
        self,
        agent_id: str,
        arguments: list[str],
        *,
        purpose: str,
        timeout: float = 45.0,
        expect_json: bool = False,
    ) -> dict[str, Any] | None:
        _, _, linux_user, _ = self._openclaw_telegram_agent_context(
            agent_id,
            purpose=purpose,
        )
        executable = self._resolve_provider_executable("openclaw")
        command = self._wrap_user_command(
            [executable, *arguments],
            linux_user,
            purpose=purpose,
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(linux_user),
                timeout=max(1.0, float(timeout)),
            )
        except subprocess.TimeoutExpired as exc:
            raise SetupError(
                f"OpenClaw timed out while {purpose}; run "
                f"'sudo clawie channel telegram status {agent_id}' to retry the probe"
            ) from exc
        if result.returncode != 0:
            # OpenClaw/provider stderr can contain request URLs. Keep the error
            # deliberately generic so a bot token can never reach the terminal,
            # event log, or JSON error surface.
            raise SetupError(
                f"OpenClaw failed while {purpose} (exit {result.returncode}); run "
                f"'sudo clawie channel telegram status {agent_id}' for safe diagnostics"
            )
        if not expect_json:
            return None
        return self._parse_openclaw_json_output(result.stdout, purpose=purpose)

    @staticmethod
    def _safe_telegram_diagnostic(value: Any, *, limit: int = 500) -> str:
        text = str(value or "").strip()
        text = re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "[redacted]", text)
        return text[:limit]

    def openclaw_telegram_status(self, agent_id: str) -> dict[str, Any]:
        """Return a stable, secret-free Telegram readiness contract."""
        with self.store.read_only():
            return self._openclaw_telegram_status_read_only(agent_id)

    def _openclaw_telegram_status_read_only(self, agent_id: str) -> dict[str, Any]:
        target = str(agent_id).strip()
        payload = self._run_openclaw_agent_command(
            target,
            ["channels", "status", "--probe", "--json"],
            purpose="checking Telegram health",
            expect_json=True,
        )
        assert isinstance(payload, dict)

        channels = payload.get("channels", {})
        telegram = channels.get("telegram", {}) if isinstance(channels, dict) else {}
        if not isinstance(telegram, dict):
            telegram = {}
        accounts_root = payload.get("channelAccounts", {})
        accounts = accounts_root.get("telegram", []) if isinstance(accounts_root, dict) else []
        account = next((row for row in accounts if isinstance(row, dict)), {}) if isinstance(accounts, list) else {}

        probe = telegram.get("probe", {})
        if not isinstance(probe, dict):
            probe = {}
        account_probe = account.get("probe", {}) if isinstance(account, dict) else {}
        if not isinstance(account_probe, dict):
            account_probe = {}
        effective_probe = probe or account_probe
        bot_info = effective_probe.get("botInfo", {})
        if not isinstance(bot_info, dict):
            bot_info = {}

        configured = bool(telegram.get("configured", account.get("configured", False)))
        running = bool(telegram.get("running", account.get("running", False)))
        connected = bool(telegram.get("connected", account.get("connected", False)))
        probe_ok = bool(effective_probe.get("ok", False))
        healthy = configured and running and connected and probe_ok
        last_error = self._safe_telegram_diagnostic(
            telegram.get("lastError", account.get("lastError", effective_probe.get("error", "")))
        )
        token_source = self._safe_telegram_diagnostic(
            telegram.get("tokenSource", account.get("tokenSource", "")),
            limit=80,
        )
        token_status = self._safe_telegram_diagnostic(account.get("tokenStatus", ""), limit=80)
        bot_username = self._safe_telegram_diagnostic(
            bot_info.get("username", account.get("botUsername", "")),
            limit=80,
        )
        mode = self._safe_telegram_diagnostic(
            telegram.get("mode", account.get("mode", "")),
            limit=80,
        )

        pairing_requests: list[dict[str, str]] = []
        pairing_check_error = ""
        try:
            pairing_result = self.list_openclaw_telegram_pairings(target)
            pairing_requests = pairing_result.get("requests", [])
        except SetupError as exc:
            pairing_check_error = self._safe_telegram_diagnostic(exc)

        if not configured:
            remediation = (
                f"Run 'sudo clawie channel telegram setup {target} --token-file PATH'"
            )
        elif not running:
            remediation = f"Run 'sudo clawie agent service restart {target}'"
        elif not probe_ok:
            remediation = (
                f"Verify the BotFather token, then rerun 'sudo clawie channel telegram setup "
                f"{target} --token-file PATH --replace'"
            )
        elif not connected:
            remediation = (
                f"Run 'sudo clawie agent service restart {target}', then retry this status command"
            )
        elif pairing_requests:
            remediation = (
                f"Approve the pending sender with 'sudo clawie channel telegram "
                f"pairing-approve {target} CODE'"
            )
        else:
            remediation = ""

        return {
            "agent_id": target,
            "healthy": healthy,
            "configured": configured,
            "running": running,
            "connected": connected,
            "probe_ok": probe_ok,
            "bot_username": bot_username,
            "mode": mode,
            "token_source": token_source,
            "token_status": token_status,
            "last_error": last_error,
            "pending_pairing_count": len(pairing_requests),
            "pending_pairings": pairing_requests,
            "pairing_check_error": pairing_check_error,
            "remediation": remediation,
        }

    def list_openclaw_telegram_pairings(self, agent_id: str) -> dict[str, Any]:
        with self.store.read_only():
            return self._list_openclaw_telegram_pairings_read_only(agent_id)

    def _list_openclaw_telegram_pairings_read_only(self, agent_id: str) -> dict[str, Any]:
        target = str(agent_id).strip()
        payload = self._run_openclaw_agent_command(
            target,
            ["pairing", "list", "telegram", "--json"],
            purpose="listing Telegram pairing requests",
            expect_json=True,
        )
        assert isinstance(payload, dict)
        raw_requests = payload.get("requests", [])
        requests: list[dict[str, str]] = []
        if isinstance(raw_requests, list):
            for row in raw_requests:
                if not isinstance(row, dict):
                    continue
                metadata = row.get("meta", row.get("metadata", {}))
                if not isinstance(metadata, dict):
                    metadata = {}
                code = self._safe_telegram_diagnostic(
                    row.get("code", row.get("pairingCode", "")),
                    limit=128,
                )
                first_name = self._safe_telegram_diagnostic(metadata.get("firstName", ""), limit=80)
                last_name = self._safe_telegram_diagnostic(metadata.get("lastName", ""), limit=80)
                metadata_display_name = " ".join(
                    item for item in (first_name, last_name) if item
                )
                requests.append(
                    {
                        "code": code,
                        "sender_id": self._safe_telegram_diagnostic(
                            row.get(
                                "senderId",
                                row.get(
                                    "userId",
                                    row.get(
                                        "chatId",
                                        metadata.get("senderId", row.get("id", "")),
                                    ),
                                ),
                            ),
                            limit=128,
                        ),
                        "username": self._safe_telegram_diagnostic(
                            row.get("username", metadata.get("username", "")),
                            limit=128,
                        ),
                        "display_name": self._safe_telegram_diagnostic(
                            row.get(
                                "displayName",
                                row.get(
                                    "name",
                                    metadata.get(
                                        "displayName",
                                        metadata.get("name", metadata_display_name),
                                    ),
                                ),
                            ),
                            limit=160,
                        ),
                        "requested_at": self._safe_telegram_diagnostic(
                            row.get("requestedAt", row.get("createdAt", row.get("timestamp", ""))),
                            limit=80,
                        ),
                    }
                )
        return {"agent_id": target, "channel": "telegram", "requests": requests}

    def approve_openclaw_telegram_pairing(self, agent_id: str, code: str) -> dict[str, Any]:
        target = str(agent_id).strip()
        pairing_code = str(code).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,127}", pairing_code):
            raise ValueError("pairing code must be 4-128 letters, numbers, hyphens, or underscores")
        self._run_openclaw_agent_command(
            target,
            ["pairing", "approve", "telegram", pairing_code],
            purpose="approving a Telegram pairing request",
        )
        return {"agent_id": target, "channel": "telegram", "status": "approved"}

    def _run_openclaw_qr_command(
        self,
        agent_id: str,
        channel: str,
        arguments: list[str],
        *,
        purpose: str,
        timeout: float = 60.0,
        expect_json: bool = False,
        interactive: bool = False,
    ) -> dict[str, Any] | None:
        spec = self._openclaw_qr_channel_spec(channel)
        _, _, linux_user, _ = self._openclaw_qr_agent_context(
            agent_id,
            purpose=purpose,
            label=spec["label"],
        )
        executable = self._resolve_provider_executable("openclaw")
        command = self._wrap_user_command(
            [executable, *arguments],
            linux_user,
            purpose=purpose,
        )
        kwargs: dict[str, Any] = {
            "check": False,
            "env": self._service_env(linux_user),
        }
        if interactive:
            if not os.isatty(0) or not os.isatty(1):
                raise SetupError(
                    f"{spec['label']} login needs an interactive terminal to display a live QR code; "
                    "rerun this command directly in an SSH terminal (no plugin, credentials, service, "
                    "or Clawie state were changed)"
                )
        else:
            kwargs.update({"capture_output": True, "text": True, "timeout": max(1.0, timeout)})
        try:
            result = subprocess.run(command, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise SetupError(
                f"OpenClaw timed out while {purpose}; run 'sudo clawie channel "
                f"{channel} status {agent_id}' for safe diagnostics"
            ) from exc
        if result.returncode != 0:
            raise SetupError(
                f"OpenClaw failed while {purpose} (exit {result.returncode}); run "
                f"'sudo clawie channel {channel} status {agent_id}' for safe diagnostics"
            )
        if not expect_json:
            return None
        return self._parse_openclaw_json_output(
            str(getattr(result, "stdout", "")),
            purpose=purpose,
        )

    def _openclaw_qr_plugin_status(
        self,
        agent_id: str,
        channel: str,
    ) -> dict[str, Any]:
        spec = self._openclaw_qr_channel_spec(channel)
        payload = self._run_openclaw_qr_command(
            agent_id,
            channel,
            ["plugins", "list", "--json"],
            purpose=f"checking the {spec['label']} plugin",
            expect_json=True,
        )
        assert isinstance(payload, dict)
        plugins = payload.get("plugins", [])
        row = next(
            (
                item
                for item in plugins
                if isinstance(item, dict) and str(item.get("id", "")) == spec["plugin_id"]
            ),
            {},
        ) if isinstance(plugins, list) else {}
        return {
            "installed": bool(row),
            "enabled": bool(row.get("enabled", False)) if isinstance(row, dict) else False,
            "status": str(row.get("status", "")) if isinstance(row, dict) else "",
            "version": str(row.get("version", "")) if isinstance(row, dict) else "",
        }

    def _ensure_openclaw_qr_plugin(
        self,
        agent_id: str,
        channel: str,
        *,
        install: bool,
    ) -> dict[str, Any]:
        spec = self._openclaw_qr_channel_spec(channel)
        before = self._openclaw_qr_plugin_status(agent_id, channel)
        installed_now = False
        if not before["installed"]:
            if not install:
                raise SetupError(
                    f"the {spec['label']} plugin is not installed for '{agent_id}'; rerun without "
                    "--skip-install (no credentials, service, or Clawie state were changed)"
                )
            try:
                self._run_openclaw_qr_command(
                    agent_id,
                    channel,
                    ["plugins", "install", "--pin", spec["package"]],
                    purpose=f"installing the {spec['label']} plugin",
                    timeout=180.0,
                )
                installed_now = True
            except Exception:
                try:
                    self._run_openclaw_qr_command(
                        agent_id,
                        channel,
                        ["plugins", "uninstall", "--force", spec["plugin_id"]],
                        purpose=f"rolling back the failed {spec['label']} plugin install",
                        timeout=60.0,
                    )
                except Exception:
                    pass
                raise

        try:
            self._run_openclaw_qr_command(
                agent_id,
                channel,
                ["config", "set", f"plugins.entries.{spec['plugin_id']}.enabled", "true"],
                purpose=f"enabling the {spec['label']} plugin",
            )
        except Exception:
            if installed_now:
                try:
                    self._run_openclaw_qr_command(
                        agent_id,
                        channel,
                        ["plugins", "uninstall", "--force", spec["plugin_id"]],
                        purpose=f"rolling back the {spec['label']} plugin install",
                    )
                except Exception:
                    pass
            raise
        after = self._openclaw_qr_plugin_status(agent_id, channel)
        if not after["installed"] or not after["enabled"]:
            raise SetupError(
                f"the {spec['label']} plugin did not become enabled; no QR login or Clawie "
                "channel assignment was attempted"
            )
        after["installed_now"] = installed_now
        return after

    def openclaw_qr_channel_status(self, agent_id: str, channel: str) -> dict[str, Any]:
        with self.store.read_only():
            spec = self._openclaw_qr_channel_spec(channel)
            target = str(agent_id).strip()
            plugin = self._openclaw_qr_plugin_status(target, channel)
            if not plugin["installed"]:
                return {
                    "agent_id": target,
                    "channel": spec["canonical"],
                    "label": spec["label"],
                    "healthy": False,
                    "installed": False,
                    "enabled": False,
                    "configured": False,
                    "running": False,
                    "connected": False,
                    "probe_ok": False,
                    "account_count": 0,
                    "last_error": "",
                    "remediation": f"Run 'sudo clawie channel {channel} setup {target}'",
                }
            payload = self._run_openclaw_qr_command(
                target,
                channel,
                ["channels", "status", "--probe", "--json"],
                purpose=f"checking {spec['label']} health",
                expect_json=True,
            )
            assert isinstance(payload, dict)
            channels = payload.get("channels", {})
            root = channels.get(spec["canonical"], {}) if isinstance(channels, dict) else {}
            root = root if isinstance(root, dict) else {}
            accounts_root = payload.get("channelAccounts", {})
            accounts = (
                accounts_root.get(spec["canonical"], [])
                if isinstance(accounts_root, dict)
                else []
            )
            accounts = [item for item in accounts if isinstance(item, dict)] if isinstance(accounts, list) else []
            configured = bool(root.get("configured", False)) or any(
                bool(item.get("configured", False)) for item in accounts
            )
            running = bool(root.get("running", False)) or any(
                bool(item.get("running", False)) for item in accounts
            )
            observations = [root, *accounts]
            connected_reported = any("connected" in item for item in observations)
            connected = (
                any(bool(item.get("connected", False)) for item in observations)
                if connected_reported
                else running
            )
            probes = [root.get("probe", {})] + [item.get("probe", {}) for item in accounts]
            probe_rows = [item for item in probes if isinstance(item, dict) and item]
            probe_ok = any(bool(item.get("ok", False)) for item in probe_rows) if probe_rows else connected
            last_error = self._safe_telegram_diagnostic(root.get("lastError", ""), limit=240)
            if not last_error:
                last_error = next(
                    (
                        self._safe_telegram_diagnostic(item.get("lastError", ""), limit=240)
                        for item in accounts
                        if str(item.get("lastError", "")).strip()
                    ),
                    "",
                )
            # OpenClaw may retain a historical lastError after a successful
            # reconnect. Live connection and probe state are authoritative;
            # keep the sanitized error only as diagnostic context.
            healthy = bool(plugin["enabled"] and configured and running and connected and probe_ok)
            login_required = bool(
                re.search(
                    r"\b(?:not linked|logged out|not logged in|link required)\b",
                    last_error,
                    flags=re.IGNORECASE,
                )
            )
            if healthy:
                remediation = ""
            elif not configured:
                remediation = f"Run 'sudo clawie channel {channel} setup {target}' to scan a fresh QR code"
            elif login_required:
                remediation = f"Run 'sudo clawie channel {channel} setup {target}' to scan a fresh QR code"
            elif not running or not connected:
                remediation = f"Run 'sudo clawie agent service restart {target}', then retry status"
            else:
                remediation = f"Rerun 'sudo clawie channel {channel} setup {target}' to repair the login"
            return {
                "agent_id": target,
                "channel": spec["canonical"],
                "label": spec["label"],
                "healthy": healthy,
                "installed": bool(plugin["installed"]),
                "enabled": bool(plugin["enabled"]),
                "plugin_version": str(plugin["version"]),
                "configured": configured,
                "running": running,
                "connected": connected,
                "connected_inferred": not connected_reported,
                "login_required": login_required,
                "probe_ok": probe_ok,
                "probe_inferred": not bool(probe_rows),
                "account_count": len(accounts),
                "last_error": last_error,
                "remediation": remediation,
            }

    def setup_openclaw_qr_channel(
        self,
        agent_id: str,
        channel: str,
        *,
        account: str = "",
        install: bool = True,
        wait_seconds: float = 45.0,
    ) -> dict[str, Any]:
        spec = self._openclaw_qr_channel_spec(channel)
        target = str(agent_id).strip()
        account_id = str(account).strip()
        if account_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", account_id):
            raise ValueError("account must contain only letters, numbers, '.', '_' or '-' (max 64)")
        timeout = float(wait_seconds)
        if not math.isfinite(timeout) or not 0.0 <= timeout <= 300.0:
            raise ValueError("wait_seconds must be finite and between 0 and 300")
        agent, info, linux_user, home = self._openclaw_qr_agent_context(
            target,
            purpose=f"{spec['label']} setup",
            label=spec["label"],
        )
        existing_plugin: dict[str, Any] | None = None
        existing_status: dict[str, Any] | None = None
        try:
            plugin_probe = self._openclaw_qr_plugin_status(target, channel)
            if bool(plugin_probe.get("installed")) and bool(plugin_probe.get("enabled")):
                status_probe = self.openclaw_qr_channel_status(target, channel)
                if bool(status_probe.get("healthy")):
                    existing_plugin = plugin_probe
                    existing_status = status_probe
        except SetupError:
            pass
        interactive = os.isatty(0) and os.isatty(1)
        if existing_status is None and not interactive:
            raise SetupError(
                f"{spec['label']} setup needs an interactive terminal to display a live QR code; "
                "rerun it directly in an SSH terminal (no plugin, credentials, service, or Clawie "
                "state were changed)"
            )
        if int(info.get("gateway_port", 0) or 0) <= 0:
            raise SetupError(
                f"agent '{target}' is not fully provisioned; run 'sudo clawie agent service start "
                f"{target}' first (no plugin, credentials, service, or Clawie state were changed)"
            )
        with self._openclaw_telegram_setup_lock(home, target, spec["label"]):
            status = existing_status
            plugin = existing_plugin
            if status is not None:
                # Recheck under the per-home lock so a disconnect cannot be
                # committed from stale preflight evidence.
                status = self.openclaw_qr_channel_status(target, channel)
                if not bool(status.get("healthy")):
                    status = None
            if status is not None:
                plugin = self._openclaw_qr_plugin_status(target, channel)
                service_result = {
                    "service_status": "running",
                    "service_mode": str(info.get("service_mode", "unknown")),
                }
                resumed_existing_login = True
            else:
                if not interactive:
                    raise SetupError(
                        f"{spec['label']} was healthy during preflight but disconnected before "
                        "ownership could be recovered; rerun setup in an interactive terminal"
                    )
                was_running = self._provider_process_live("openclaw", linux_user)
                login_attempted = False
                try:
                    plugin = self._ensure_openclaw_qr_plugin(target, channel, install=install)
                    login_args = ["channels", "login", "--channel", spec["canonical"]]
                    if account_id:
                        login_args.extend(["--account", account_id])
                    login_attempted = True
                    self._run_openclaw_qr_command(
                        target,
                        channel,
                        login_args,
                        purpose=f"logging in to {spec['label']}",
                        interactive=True,
                    )
                except Exception:
                    if login_attempted:
                        try:
                            self._run_managed_provider_service_action(
                                provider="openclaw",
                                action="restart" if was_running else "stop",
                                linux_user=linux_user,
                                agent_info=info,
                            )
                        except Exception:
                            raise SetupError(
                                f"{spec['label']} login failed and automatic gateway recovery also "
                                f"failed; run 'sudo clawie agent service status {target}', then "
                                f"'sudo clawie agent service {'restart' if was_running else 'stop'} "
                                f"{target}'"
                            ) from None
                    raise
                service_result = self._run_managed_provider_service_action(
                    provider="openclaw",
                    action="restart" if was_running else "start",
                    linux_user=linux_user,
                    agent_info=info,
                )
                deadline = time.monotonic() + timeout
                status = None
                while True:
                    try:
                        status = self.openclaw_qr_channel_status(target, channel)
                    except SetupError:
                        status = None
                    if status and bool(status.get("healthy")):
                        break
                    if time.monotonic() >= deadline:
                        detail = str((status or {}).get("last_error", "")).strip()
                        suffix = f" Last error: {detail}." if detail else ""
                        raise SetupError(
                            f"{spec['label']} login completed but live health did not become ready.{suffix} "
                            f"Credentials were preserved; rerun 'sudo clawie channel {channel} status {target}' "
                            "before deciding whether to relink. Clawie channel ownership was not changed."
                        )
                    time.sleep(1.0)
                resumed_existing_login = False

            assert plugin is not None
            assert status is not None

            state = self.store.read_state()
            agents = state.setdefault("agents", {})
            persisted = agents.get(target)
            if not isinstance(persisted, dict):
                raise AgentNotFoundError(f"agent not found: {target}")
            channel_name = spec["canonical"]
            self._assert_channels_unclaimed(
                agents,
                target,
                [{"kind": spec["canonical"], "name": channel_name}],
            )
            rows = persisted.setdefault("channels", [])
            if not isinstance(rows, list):
                rows = []
                persisted["channels"] = rows
            index = self._find_channel(rows, spec["canonical"], channel_name)
            if index is None:
                rows.append(
                    {
                        "kind": spec["canonical"],
                        "name": channel_name,
                        "enabled": True,
                        "external_id": f"{target}:{spec['canonical']}:{account_id or 'default'}",
                    }
                )
            else:
                rows[index]["enabled"] = True
            persisted_info = persisted.setdefault("agent", {})
            persisted_info["service_status"] = str(service_result.get("service_status", "running"))
            persisted_info["service_mode"] = str(service_result.get("service_mode", "unknown"))
            persisted_info["last_sync"] = now_iso()
            self._event(
                state,
                f"channels.{spec['canonical']}_configured",
                f"Configured {spec['label']} for {target}",
                {
                    "agent_id": target,
                    "provider": "openclaw",
                    "kind": spec["canonical"],
                    "name": channel_name,
                    "account": account_id or "default",
                    "plugin_version": str(plugin.get("version", "")),
                },
            )
            self.store.write_state(state)
            return {
                "agent_id": target,
                "channel": spec["canonical"],
                "label": spec["label"],
                "account": account_id or "default",
                "plugin": plugin,
                "status": status,
                "resumed_existing_login": resumed_existing_login,
            }

    def list_openclaw_qr_pairings(self, agent_id: str, channel: str) -> dict[str, Any]:
        spec = self._openclaw_qr_channel_spec(channel)
        payload = self._run_openclaw_qr_command(
            agent_id,
            channel,
            ["pairing", "list", spec["canonical"], "--json"],
            purpose=f"listing {spec['label']} pairing requests",
            expect_json=True,
        )
        assert isinstance(payload, dict)
        requests: list[dict[str, str]] = []
        for row in payload.get("requests", []):
            if not isinstance(row, dict):
                continue
            requests.append(
                {
                    "code": self._safe_telegram_diagnostic(
                        row.get("code", row.get("pairingCode", "")), limit=128
                    ),
                    "sender_id": self._safe_telegram_diagnostic(
                        row.get("senderId", row.get("userId", row.get("id", ""))), limit=128
                    ),
                    "display_name": self._safe_telegram_diagnostic(
                        row.get("displayName", row.get("name", "")), limit=160
                    ),
                    "requested_at": self._safe_telegram_diagnostic(
                        row.get("requestedAt", row.get("createdAt", "")), limit=80
                    ),
                }
            )
        return {
            "agent_id": str(agent_id).strip(),
            "channel": spec["canonical"],
            "requests": requests,
        }

    def approve_openclaw_qr_pairing(
        self,
        agent_id: str,
        channel: str,
        code: str,
    ) -> dict[str, Any]:
        spec = self._openclaw_qr_channel_spec(channel)
        pairing_code = str(code).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,127}", pairing_code):
            raise ValueError("pairing code must be 4-128 letters, numbers, hyphens, or underscores")
        self._run_openclaw_qr_command(
            agent_id,
            channel,
            ["pairing", "approve", spec["canonical"], pairing_code],
            purpose=f"approving a {spec['label']} pairing request",
        )
        return {
            "agent_id": str(agent_id).strip(),
            "channel": spec["canonical"],
            "status": "approved",
        }

    def configure_openclaw_telegram(
        self,
        agent_id: str,
        token: str,
        *,
        wait_seconds: float = 30.0,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Serialize and execute one rollback-safe Telegram setup transaction."""
        target = str(agent_id).strip()
        bot_token = str(token).strip()
        if not self._looks_like_telegram_bot_token(bot_token):
            raise ValueError("invalid Telegram bot token; copy the complete token from BotFather")
        wait_timeout = float(wait_seconds)
        if not math.isfinite(wait_timeout) or not 0.0 <= wait_timeout <= 300.0:
            raise ValueError("wait_seconds must be finite and between 0 and 300")
        _, _, _, home = self._openclaw_telegram_agent_context(
            target,
            purpose="Telegram setup",
        )
        with self._openclaw_telegram_setup_lock(home, target):
            return self._configure_openclaw_telegram_locked(
                target,
                bot_token,
                wait_seconds=wait_timeout,
                replace=replace,
            )

    def _configure_openclaw_telegram_locked(
        self,
        agent_id: str,
        token: str,
        *,
        wait_seconds: float = 30.0,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Configure Telegram as a rollback-safe transaction."""
        target = str(agent_id).strip()
        bot_token = str(token).strip()
        if not self._looks_like_telegram_bot_token(bot_token):
            raise ValueError("invalid Telegram bot token; copy the complete token from BotFather")
        wait_timeout = float(wait_seconds)
        if not math.isfinite(wait_timeout) or not 0.0 <= wait_timeout <= 300.0:
            raise ValueError("wait_seconds must be finite and between 0 and 300")
        agent, info, linux_user, home = self._openclaw_telegram_agent_context(
            target,
            purpose="Telegram setup",
        )
        gateway_port = int(info.get("gateway_port", 0) or 0)
        gateway_token = str(info.get("gateway_token", "")).strip()
        if gateway_port <= 0 or not gateway_token:
            raise SetupError(
                f"agent '{target}' is not fully provisioned; run "
                f"'sudo clawie agent service start {target}' first "
                "(no files, services, or agent state were changed)"
            )

        # Resolve and version-gate the runtime before touching the managed home.
        executable = self._resolve_provider_executable("openclaw")
        self._verify_installed_runtime_version("openclaw", executable)

        state_before = self.store.read_state()
        channel_name = self._telegram_channel_name_for_agent(state_before, target)
        try:
            token_before = self._read_agent_text_file(
                home,
                ".openclaw/telegram.token",
                max_bytes=4096,
            )
            token_existed = True
        except FileNotFoundError:
            token_before = ""
            token_existed = False
        try:
            config_before = self._read_agent_text_file(
                home,
                ".openclaw/openclaw.json",
                max_bytes=16 * 1024 * 1024,
            )
            config_existed = True
        except FileNotFoundError:
            config_before = ""
            config_existed = False
        config = self._parse_existing_openclaw_config(config_before, exists=config_existed)
        already_configured, old_token = self._existing_telegram_token(
            config,
            token_before,
            token_file_exists=token_existed,
        )
        same_token = bool(old_token) and hmac.compare_digest(old_token, bot_token)
        if already_configured and not same_token and not bool(replace):
            raise SetupError(
                f"Telegram is already configured for '{target}'; rerun with --replace to "
                "confirm changing the bot identity (no files, services, or agent state were changed)"
            )

        # A well-shaped token is not necessarily valid. Validate identity before
        # the first write or service operation so bad secrets cannot take a
        # working bot offline.
        token_identity = self._probe_telegram_bot_token(bot_token)
        was_running = self._provider_process_live("openclaw", linux_user)
        detached_agent = copy.deepcopy(agent)
        self._hydrate_agent_controls(detached_agent)
        detached_info = detached_agent.setdefault("agent", {})
        detached_channels = detached_agent.setdefault("channels", [])
        if not isinstance(detached_channels, list):
            detached_channels = []
            detached_agent["channels"] = detached_channels
        channel_index = self._find_channel(detached_channels, "telegram", channel_name)
        if channel_index is None:
            detached_channels.append(
                {
                    "kind": "telegram",
                    "name": channel_name,
                    "enabled": True,
                    "external_id": f"{target}:telegram:{len(detached_channels) + 1}",
                }
            )
        else:
            detached_channels[channel_index]["enabled"] = True

        token_path = home / ".openclaw" / "telegram.token"
        action = "restart" if was_running else "start"
        service_touched = False
        service_result: dict[str, Any] = {}
        status: dict[str, Any] | None = None
        dm_policy = "pairing"
        try:
            token_path = self._write_agent_text_file(
                home,
                ".openclaw/telegram.token",
                bot_token + "\n",
                linux_user,
                mode=0o600,
            )
            _, dm_policy = self._write_scoped_openclaw_telegram_config(
                home=home,
                linux_user=linux_user,
                config=config,
                token_path=token_path,
            )
            service_touched = True
            service_result = self._run_managed_provider_service_action(
                provider="openclaw",
                action=action,
                linux_user=linux_user,
                agent_info=detached_info,
            )
            if str(service_result.get("service_status", "")).strip().lower() != "running":
                raise SetupError("OpenClaw did not report a running service after Telegram setup")
            self._assert_provider_postflight_ready(
                provider="openclaw",
                linux_user=linux_user,
                home=home,
                auth_mode=str(detached_info.get("auth_mode", "")),
            )

            deadline = time.monotonic() + wait_timeout
            while True:
                try:
                    status = self.openclaw_telegram_status(target)
                except SetupError:
                    status = None
                if status and bool(status.get("healthy")):
                    break
                if time.monotonic() >= deadline:
                    detail = self._safe_telegram_diagnostic(
                        (status or {}).get("last_error", ""),
                        limit=240,
                    )
                    suffix = f" Last error: {detail}." if detail else ""
                    raise SetupError(f"Telegram's live probe did not become healthy.{suffix}")
                time.sleep(1.0)

            # Commit ownership and runtime metadata only after live health is
            # proven. Re-read to retain unrelated concurrent updates; the
            # store's revision check still rejects a race during this commit.
            state = self.store.read_state()
            agents = state.setdefault("agents", {})
            persisted = agents.get(target)
            if not persisted:
                raise AgentNotFoundError(f"agent not found: {target}")
            self._assert_channels_unclaimed(
                agents,
                target,
                [{"kind": "telegram", "name": channel_name}],
            )
            self._hydrate_agent_controls(persisted)
            persisted_channels = persisted.setdefault("channels", [])
            if not isinstance(persisted_channels, list):
                persisted_channels = []
                persisted["channels"] = persisted_channels
            persisted_index = self._find_channel(
                persisted_channels,
                "telegram",
                channel_name,
            )
            if persisted_index is None:
                persisted_channels.append(
                    {
                        "kind": "telegram",
                        "name": channel_name,
                        "enabled": True,
                        "external_id": f"{target}:telegram:{len(persisted_channels) + 1}",
                    }
                )
            else:
                persisted_channels[persisted_index]["enabled"] = True
            persisted_info = persisted.setdefault("agent", {})
            for key in ("gateway_port", "gateway_token"):
                persisted_info[key] = detached_info[key]
            persisted_info["service_status"] = str(
                service_result.get("service_status", "running")
            )
            persisted_info["service_mode"] = str(
                service_result.get("service_mode", "unknown")
            )
            if "fallback_pid" in persisted_info or int(
                service_result.get("fallback_pid", 0) or 0
            ) > 0:
                persisted_info["fallback_pid"] = int(
                    service_result.get("fallback_pid", 0) or 0
                )
            persisted_info["last_sync"] = now_iso()
            self._event(
                state,
                "channels.telegram_configured",
                f"Configured private Telegram token file for {target}",
                {
                    "agent_id": target,
                    "provider": "openclaw",
                    "kind": "telegram",
                    "name": channel_name,
                    "token_source": "tokenFile",
                    "dm_policy": dm_policy,
                    "bot_id": token_identity.get("bot_id", ""),
                    "bot_username": token_identity.get("bot_username", ""),
                    "replaced": bool(already_configured and not same_token),
                },
            )
            self.store.write_state(state)
        except BaseException as exc:
            rollback_errors: list[str] = []
            try:
                self._restore_telegram_files(
                    home=home,
                    linux_user=linux_user,
                    token_existed=token_existed,
                    token_content=token_before,
                    config_existed=config_existed,
                    config_content=config_before,
                )
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(
                    "could not restore the previous Telegram files: "
                    + self._safe_telegram_diagnostic(rollback_exc, limit=180)
                )
            if service_touched:
                rollback_action = "restart" if was_running else "stop"
                try:
                    rollback_result = self._run_managed_provider_service_action(
                        provider="openclaw",
                        action=rollback_action,
                        linux_user=linux_user,
                        agent_info=info,
                    )
                    expected_status = "running" if was_running else "stopped"
                    if str(rollback_result.get("service_status", "")).strip().lower() != expected_status:
                        rollback_errors.append(
                            f"the prior service state was not restored ({expected_status} expected)"
                        )
                    elif was_running:
                        self._assert_provider_postflight_ready(
                            provider="openclaw",
                            linux_user=linux_user,
                            home=home,
                            auth_mode=str(info.get("auth_mode", "")),
                        )
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(
                        "could not restore the prior service state: "
                        + self._safe_telegram_diagnostic(rollback_exc, limit=180)
                    )
            reason = self._safe_telegram_diagnostic(exc, limit=240)
            if rollback_errors:
                joined = "; ".join(rollback_errors)
                raise SetupError(
                    f"Telegram setup failed ({reason}). Automatic rollback was incomplete: {joined}. "
                    f"Run 'sudo clawie channel telegram status {target}' before retrying."
                ) from None
            raise SetupError(
                f"Telegram setup failed ({reason}). The previous files and service state were restored; "
                "agent state was not changed."
            ) from None
        finally:
            bot_token = ""

        assert status is not None
        return {
            "agent_id": target,
            "status": status,
            "service_action": str(service_result.get("action", action)),
            "token_file": str(token_path),
            "dm_policy": dm_policy,
            "channel_name": channel_name,
            "replaced": bool(already_configured and not same_token),
        }

    def toggle_agent_channel(self, agent_id: str, channel_index: int) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        channels = agent.get("channels", [])
        if not isinstance(channels, list) or channel_index < 0 or channel_index >= len(channels):
            raise ValueError("invalid channel selection")

        selected = channels[channel_index]
        selected["enabled"] = not bool(selected.get("enabled", True))
        agent_info = agent.setdefault("agent", {})
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.channel_toggled",
            f"Toggled channel {selected.get('name', '')} for {agent_id}",
            {
                "agent_id": agent_id,
                "channel_name": str(selected.get("name", "")),
                "enabled": bool(selected.get("enabled", True)),
            },
        )
        self.store.write_state(state)
        return agent

    def _reconnect_agent_channels(
        self,
        *,
        provider: str,
        linux_user: str,
        channels: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        reconnectable: list[dict[str, str]] = []
        if str(provider).strip().lower() in {"picoclaw", "openclaw"}:
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                if not bool(channel.get("enabled", True)):
                    continue
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name or kind == "cli" or kind != "telegram":
                    continue
                reconnectable.append({"kind": kind, "name": name})
            return reconnectable

        commands: list[list[str]] = []
        seen_commands: set[tuple[str, ...]] = set()
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            if not bool(channel.get("enabled", True)):
                continue
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name or kind == "cli":
                continue
            reconnectable.append({"kind": kind, "name": name})
            for cmd in self._channel_connect_commands(provider, kind, name, linux_user):
                key = tuple(cmd)
                if key in seen_commands:
                    continue
                seen_commands.add(key)
                commands.append(cmd)

        env = self._service_env(linux_user)
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                raise SetupError(
                    f"channel reconnect failed for {provider}: {output or f'exit {result.returncode}'}"
                )
        return reconnectable

    def _effective_agent_channels(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        view = self._attach_agent_channel_view(copy.deepcopy(payload))
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for channel in view.get("channels", []):
            if not isinstance(channel, dict):
                continue
            if not bool(channel.get("enabled", True)):
                continue
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kind": kind,
                    "name": name,
                    "enabled": True,
                    "external_id": str(channel.get("external_id", "")).strip(),
                    "discovered_provider": str(channel.get("discovered_provider", "")).strip().lower(),
                }
            )
        return rows

    def _persist_effective_agent_channels(
        self,
        payload: dict[str, Any],
        channels: list[dict[str, Any]],
    ) -> None:
        agent_id = str(payload.get("agent_id", payload.get("user_id", ""))).strip()
        existing_rows = payload.get("channels", [])
        existing_map: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(existing_rows, list):
            for row in existing_rows:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key[0] and key[1]:
                    existing_map[key] = dict(row)

        persisted: list[dict[str, Any]] = []
        for idx, channel in enumerate(channels, start=1):
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            row = dict(existing_map.get(key, {}))
            row["kind"] = kind
            row["name"] = name
            row["enabled"] = bool(channel.get("enabled", True))
            external_id = str(channel.get("external_id", row.get("external_id", ""))).strip()
            if external_id:
                row["external_id"] = external_id
            elif agent_id:
                row["external_id"] = f"{agent_id}:{kind}:{idx}"
            row.pop("channel_source", None)
            row.pop("discovered_provider", None)
            persisted.append(row)
        payload["channels"] = persisted

    def _provider_channel_payloads_for_home(
        self,
        provider: str,
        root: Path,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        name = str(provider).strip().lower()
        if name == "zeroclaw":
            return self._read_zeroclaw_channel_payloads(root)
        if name == "picoclaw":
            return self._read_picoclaw_channel_payloads(root)
        if name == "openclaw":
            return self._read_openclaw_channel_payloads(root)
        return {}

    def _read_zeroclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "config.toml"
        if tomllib is None or not config_path.exists():
            return {}
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        channels_cfg = payload.get("channels_config", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        if bool(channels_cfg.get("cli")):
            rows[("cli", "local")] = {
                "kind": "cli",
                "name": "local",
                "provider": "zeroclaw",
                "settings": {"enabled": True},
            }
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if kind == "cli" or not kind or not isinstance(value, dict):
                continue
            if not bool(value.get("enabled", True)):
                continue
            name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "zeroclaw",
                "settings": dict(value),
            }
        return rows

    def _read_picoclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "config.json"
        payload = self._read_json_file(config_path)
        channels_cfg = payload.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if not kind:
                continue
            if isinstance(value, dict):
                enabled = bool(value.get("enabled", True))
                name = str(value.get("name", kind)).strip().lower() or kind
                settings = dict(value)
            else:
                enabled = bool(value)
                name = kind
                settings = {"enabled": enabled}
            if not enabled:
                continue
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "picoclaw",
                "settings": settings,
            }
        return rows

    def _read_openclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "openclaw.json"
        payload = self._read_json_file(config_path)
        channels_cfg = payload.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if kind == "defaults":
                continue
            if not kind or not isinstance(value, dict):
                continue
            if not bool(value.get("enabled", True)):
                continue
            if is_openclaw_channel_placeholder(value):
                continue

            settings = dict(value)
            settings.pop("accounts", None)
            name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "openclaw",
                "settings": settings,
            }

            accounts = value.get("accounts", {})
            if not isinstance(accounts, dict):
                continue
            for account_id, account_value in accounts.items():
                if not isinstance(account_value, dict):
                    continue
                if not bool(account_value.get("enabled", value.get("enabled", True))):
                    continue
                account_name = (
                    str(account_value.get("name", account_id)).strip().lower().replace(" ", "-")
                    or str(account_id).strip().lower()
                    or kind
                )
                account_settings = dict(settings)
                account_settings.update(account_value)
                rows[(kind, account_name)] = {
                    "kind": kind,
                    "name": account_name,
                    "provider": "openclaw",
                    "settings": account_settings,
                }
        return rows

    def _can_read_provider_channel_roots(self, home: Path, providers: list[str]) -> bool:
        for item in providers:
            token = str(item).strip().lower()
            if not token:
                continue
            try:
                root = home / get_provider(token).state_dir
            except ValueError:
                continue
            if root.exists() and os.access(root, os.R_OK | os.X_OK):
                return True
        return False

    def _discover_live_channel_payloads(self, payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        info = payload.get("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        is_local = bool(info.get("local_user", False))
        provider = str(info.get("provider", "")).strip().lower()
        home = self._local_agent_home(provider) if is_local else self._agent_linux_home(payload)
        if not home:
            return {}
        if linux_user and not is_local and not self._can_manage_linux_user(linux_user):
            if not self._can_read_provider_channel_roots(home, [provider, *provider_names()]):
                return {}

        ordered: list[str] = []
        seen_providers: set[str] = set()
        for item in [provider] + provider_names():
            token = str(item).strip().lower()
            if not token or token in seen_providers:
                continue
            seen_providers.add(token)
            ordered.append(token)

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for name in ordered:
            root = home / get_provider(name).state_dir
            for key, value in self._provider_channel_payloads_for_home(name, root).items():
                rows.setdefault(key, value)
        return rows

    @staticmethod
    def _looks_like_unresolved_secret(value: str) -> bool:
        token = str(value).strip()
        if not token:
            return False
        if re.search(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*", token):
            return True
        lowered = token.lower()
        if lowered.startswith("env:") or lowered.startswith("secret:"):
            return True
        return "{{" in token and "}}" in token

    @staticmethod
    def _looks_like_telegram_bot_token(value: str) -> bool:
        token = str(value).strip()
        return bool(re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token))

    def migrate_channels(
        self,
        from_agent: str,
        to_agent: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        if from_agent == to_agent:
            raise ValueError("from_agent and to_agent must differ")
        state = self.store.read_state()

        agents = state.setdefault("agents", {})
        source = agents.get(from_agent)
        target = agents.get(to_agent)
        if not source:
            raise AgentNotFoundError(f"source agent not found: {from_agent}")
        if not target:
            raise AgentNotFoundError(f"target agent not found: {to_agent}")

        source_channels = copy.deepcopy(source.get("channels", []))
        for channel in source_channels:
            channel["migrated_from"] = from_agent
        source_keys = self._channel_keys(source_channels)
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=to_agent,
            channels=source_channels,
            allow_owners={from_agent, to_agent},
        )

        if replace:
            target_channels = source_channels
        else:
            target_channels = copy.deepcopy(target.get("channels", []))
            existing = {(row.get("kind", ""), row.get("name", "")) for row in target_channels}
            for channel in source_channels:
                key = (channel.get("kind", ""), channel.get("name", ""))
                if key not in existing:
                    target_channels.append(channel)
                    existing.add(key)

        target["channels"] = target_channels
        moved_from_source = self._remove_channel_keys_from_agent(source=source, keys=source_keys)
        if moved_from_source:
            source.setdefault("agent", {})["last_sync"] = now_iso()
        target["channel_strategy"] = "migrate"
        # The migration is complete before this state is persisted; runtime
        # liveness is reported separately by service probes.
        target["agent"]["status"] = "configured"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.migrated",
            f"Migrated channels from {from_agent} to {to_agent}",
            {
                "from_agent": from_agent,
                "to_agent": to_agent,
                "replace": replace,
                "channel_count": len(target_channels),
                "moved_from_source": moved_from_source,
            },
        )
        self.store.write_state(state)
        return target

    def bootstrap_channels(
        self,
        agent_id: str,
        preset: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        presets = {
            "minimal": [{"kind": "chat", "name": "primary"}],
            "growth": [
                {"kind": "chat", "name": "support"},
                {"kind": "email", "name": "inbox"},
                {"kind": "social", "name": "community"},
            ],
            "enterprise": [
                {"kind": "chat", "name": "ops"},
                {"kind": "email", "name": "queue"},
                {"kind": "voice", "name": "contact-center"},
                {"kind": "ticketing", "name": "service-desk"},
            ],
        }
        if preset not in presets:
            raise ValueError("preset must be one of: minimal, growth, enterprise")

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        target = agents.get(agent_id)
        if not target:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        generated = self._mint_channels(agent_id, presets[preset])
        if replace:
            target_channels = generated
        else:
            target_channels = copy.deepcopy(target.get("channels", []))
            existing = {(row.get("kind", ""), row.get("name", "")) for row in target_channels}
            for channel in generated:
                key = (channel.get("kind", ""), channel.get("name", ""))
                if key not in existing:
                    target_channels.append(channel)
                    existing.add(key)
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=agent_id,
            channels=target_channels,
        )

        target["channels"] = target_channels
        target["agent"]["status"] = "configured"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.bootstrapped",
            f"Applied {preset} channel preset for {agent_id}",
            {
                "agent_id": agent_id,
                "preset": preset,
                "replace": replace,
                "channel_count": len(target_channels),
            },
        )
        self.store.write_state(state)
        return target

    def channel_inventory(self) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        rows: list[dict[str, Any]] = []
        assigned_keys: set[tuple[str, str]] = set()
        for aid, payload in sorted(agents.items()):
            view = self._attach_agent_channel_view(copy.deepcopy(payload))
            provider = str(view.get("agent", {}).get("provider", "")).strip().lower()
            for channel in view.get("channels", []):
                if not isinstance(channel, dict):
                    continue
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name:
                    continue
                assigned_keys.add((kind, name))
                rows.append(
                    {
                        "source": str(channel.get("channel_source", "agent")) or "agent",
                        "owner_agent_id": str(aid),
                        "provider": provider,
                        "kind": kind,
                        "name": name,
                        "enabled": bool(channel.get("enabled", True)),
                        "discovered_provider": str(channel.get("discovered_provider", "")),
                    }
                )

        for channel in self._read_channel_pool():
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name or (kind, name) in assigned_keys:
                continue
            rows.append(
                {
                    "source": "pool",
                    "owner_agent_id": "@pool",
                    "provider": str(channel.get("provider", "")).strip().lower(),
                    "kind": kind,
                    "name": name,
                    "enabled": False,
                }
            )

        for item in self._local_channel_inventory():
            key = (str(item.get("kind", "")).strip().lower(), str(item.get("name", "")).strip())
            if key in assigned_keys:
                continue
            rows.append(item)

        kinds = {str(row.get("kind", "")) for row in rows if str(row.get("kind", "")).strip()}
        return {
            "generated_at": now_iso(),
            "rows": rows,
            "totals": {
                "channels": len(rows),
                "kinds": len(kinds),
                "assigned": sum(
                    1 for row in rows if str(row.get("owner_agent_id", "")).strip() not in {"", "@pool"}
                ),
                "local": sum(1 for row in rows if str(row.get("source", "")) == "local"),
                "pool": sum(1 for row in rows if str(row.get("source", "")) == "pool"),
            },
        }

    def assign_channel_to_agent(
        self,
        source_agent_id: str,
        kind: str,
        name: str,
        target_agent_id: str,
    ) -> dict[str, Any]:
        self._require_setup()
        src = str(source_agent_id).strip()
        dst = str(target_agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")
        if not dst:
            raise ValueError("target_agent_id is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        target = agents.get(dst)
        if not target:
            raise AgentNotFoundError(f"target agent not found: {dst}")
        self._hydrate_agent_controls(target)
        target_channels = target.setdefault("channels", [])
        if not isinstance(target_channels, list):
            target_channels = []
            target["channels"] = target_channels

        moved_from_agents = self._remove_channel_from_other_agents(
            agents=agents,
            kind=channel_kind,
            name=channel_name,
            keep_agent_id=dst,
        )
        self._remove_pool_channel(channel_kind, channel_name)
        if self._find_channel(target_channels, channel_kind, channel_name) is None:
            target_channels.append(
                {
                    "kind": channel_kind,
                    "name": channel_name,
                    "enabled": True,
                    "external_id": f"{dst}:{channel_kind}:{len(target_channels) + 1}",
                }
            )

        moved = bool(moved_from_agents)

        target.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "channels.assigned",
            f"Assigned channel {channel_kind}:{channel_name} to {dst}",
            {
                "source_agent_id": src,
                "target_agent_id": dst,
                "kind": channel_kind,
                "name": channel_name,
                "moved": moved,
                "moved_from_agent_ids": moved_from_agents,
            },
        )
        self.store.write_state(state)
        return {
            "source_agent_id": src,
            "target_agent_id": dst,
            "kind": channel_kind,
            "name": channel_name,
            "moved": moved,
            "moved_from_agent_ids": moved_from_agents,
        }

    def unassign_channel_from_agent(
        self,
        agent_id: str,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        self._require_setup()
        src = str(agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not src:
            raise ValueError("agent_id is required")
        if src.startswith("@local:"):
            raise ValueError("cannot unassign local-user channel")
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        source = agents.get(src)
        if not source:
            raise AgentNotFoundError(f"agent not found: {src}")
        self._hydrate_agent_controls(source)
        channels = source.setdefault("channels", [])
        if not isinstance(channels, list):
            channels = []
            source["channels"] = channels

        found_idx = self._find_channel(channels, channel_kind, channel_name)
        if found_idx is None:
            raise ValueError(f"channel not found on {src}: {channel_kind}:{channel_name}")
        removed = channels.pop(found_idx)
        source.setdefault("agent", {})["last_sync"] = now_iso()

        pool = self._read_channel_pool()
        if self._find_channel(pool, channel_kind, channel_name) is None:
            pool.append(
                {
                    "kind": channel_kind,
                    "name": channel_name,
                    "provider": str(source.get("agent", {}).get("provider", "")).strip().lower(),
                    "external_id": str(removed.get("external_id", "")),
                }
            )
            self._write_channel_pool(pool)

        provider = str(source.get("agent", {}).get("provider", "")).strip().lower()
        linux_user = str(source.get("agent", {}).get("linux_user", "")).strip()
        home = self._agent_linux_home(source)
        if provider in {"picoclaw", "openclaw"} and linux_user and home:
            self._prepare_agent_provider_home(
                provider=provider,
                agent=source,
                linux_user=linux_user,
                home=home,
                channels=self._effective_agent_channels(source),
                live_payloads=self._discover_live_channel_payloads(source),
            )
            if provider == "picoclaw":
                self._remove_picoclaw_channel_from_home(home=home, linux_user=linux_user, kind=channel_kind)
            else:
                self._remove_openclaw_channel_from_home(home=home, linux_user=linux_user, kind=channel_kind)
            if self._provider_process_live(provider, linux_user):
                result = self._run_managed_provider_service_action(
                    provider=provider,
                    action="restart",
                    linux_user=linux_user,
                    agent_info=source.setdefault("agent", {}),
                )
                source["agent"]["service_status"] = str(result.get("service_status", "unknown"))
                source["agent"]["service_mode"] = str(result.get("service_mode", "unknown"))

        self._event(
            state,
            "channels.unassigned",
            f"Unassigned channel {channel_kind}:{channel_name} from {src}",
            {
                "source_agent_id": src,
                "kind": channel_kind,
                "name": channel_name,
            },
        )
        self.store.write_state(state)
        return {
            "source_agent_id": src,
            "kind": channel_kind,
            "name": channel_name,
            "status": "unassigned",
        }

    def connect_agent_channel(
        self,
        agent_id: str,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        self._require_setup()
        target = str(agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not target:
            raise ValueError("agent_id is required")
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")
        if target.startswith("@local:"):
            raise ValueError("connect is only supported for managed agents")
        self._refresh_managed_agent_provider_alignment(target)

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(target)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {target}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{target}' has no provider configured")
        linux_user = str(info.get("linux_user", "")).strip()
        existing_channels = agent.get("channels", [])
        already_assigned = (
            isinstance(existing_channels, list)
            and self._find_channel(existing_channels, channel_kind, channel_name) is not None
        )
        if not already_assigned:
            self.assign_channel_to_agent("", channel_kind, channel_name, target)
            state = self.store.read_state()
            agents = state.setdefault("agents", {})
            agent = agents.get(target)
            if not agent:
                raise AgentNotFoundError(f"agent not found: {target}")
            self._hydrate_agent_controls(agent)
            info = agent.setdefault("agent", {})
            provider = str(info.get("provider", "")).strip().lower()
            linux_user = str(info.get("linux_user", "")).strip()

        if provider == "picoclaw":
            home = self._agent_linux_home(agent)
            effective_channels = self._effective_agent_channels(agent)
            live_payloads = self._discover_live_channel_payloads(agent)
            if home:
                self._write_prompt_files_for_home(
                    provider, home, agent.get("core_prompts", {}), linux_user,
                )
            self._prepare_agent_provider_home(
                provider=provider,
                agent=agent,
                linux_user=linux_user,
                home=home,
                channels=effective_channels,
                live_payloads=live_payloads,
            )
            if self._provider_process_live(provider, linux_user):
                result = self._run_managed_provider_service_action(
                    provider=provider,
                    action="restart",
                    linux_user=linux_user,
                    agent_info=info,
                )
                info["service_status"] = str(result.get("service_status", "unknown"))
                info["service_mode"] = str(result.get("service_mode", "unknown"))
            info["last_sync"] = now_iso()
            self._event(
                state,
                "channels.connected",
                f"Connected channel {channel_kind}:{channel_name} for {target}",
                {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": "config-write",
                },
            )
            self.store.write_state(state)
            return {
                "agent_id": target,
                "provider": provider,
                "kind": channel_kind,
                "name": channel_name,
                "command": [],
                "output": "configured provider channel",
                "status": "connected",
            }
        if provider == "openclaw":
            home = self._agent_linux_home(agent)
            effective_channels = self._effective_agent_channels(agent)
            live_payloads = self._discover_live_channel_payloads(agent)
            if channel_kind == "telegram" and (
                live_payloads.get((channel_kind, channel_name))
                or any(str(key[0]).strip().lower() == channel_kind for key in live_payloads)
            ):
                if home:
                    self._write_prompt_files_for_home(
                        provider, home, agent.get("core_prompts", {}), linux_user,
                    )
                self._prepare_agent_provider_home(
                    provider=provider,
                    agent=agent,
                    linux_user=linux_user,
                    home=home,
                    channels=effective_channels,
                    live_payloads=live_payloads,
                )
                if self._provider_process_live(provider, linux_user):
                    result = self._run_managed_provider_service_action(
                        provider=provider,
                        action="restart",
                        linux_user=linux_user,
                        agent_info=info,
                    )
                    info["service_status"] = str(result.get("service_status", "unknown"))
                    info["service_mode"] = str(result.get("service_mode", "unknown"))
                info["last_sync"] = now_iso()
                self._event(
                    state,
                    "channels.connected",
                    f"Connected channel {channel_kind}:{channel_name} for {target}",
                    {
                        "agent_id": target,
                        "provider": provider,
                        "kind": channel_kind,
                        "name": channel_name,
                        "command": "config-write",
                    },
                )
                self.store.write_state(state)
                return {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": [],
                    "output": "configured provider channel",
                    "status": "connected",
                }

        commands = self._channel_connect_commands(provider, channel_kind, channel_name, linux_user)
        last_error = ""
        env = self._service_env(linux_user)
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                state = self.store.read_state()
                agents = state.setdefault("agents", {})
                refreshed = agents.get(target, {})
                refreshed_info = refreshed.setdefault("agent", {})
                refreshed_info["last_sync"] = now_iso()
                self._event(
                    state,
                    "channels.connected",
                    f"Connected channel {channel_kind}:{channel_name} for {target}",
                    {
                        "agent_id": target,
                        "provider": provider,
                        "kind": channel_kind,
                        "name": channel_name,
                        "command": " ".join(cmd),
                    },
                )
                self.store.write_state(state)
                return {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": cmd,
                    "output": output,
                    "status": "connected",
                }
            last_error = output or f"exit {result.returncode}"

        if not already_assigned:
            try:
                self.unassign_channel_from_agent(target, channel_kind, channel_name)
            except Exception:
                pass

        raise SetupError(
            f"channel connect failed for {target} ({provider}): {last_error}. "
            + ("attempted: " + " || ".join(" ".join(cmd) for cmd in commands) if commands else "")
        )

    def sync_agent_channels_from_provider(self, agent_id: str, *, replace: bool = True) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            raise ValueError("channel sync is only supported for managed agents")
        self._refresh_managed_agent_provider_alignment(token)
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        discovery = self._discover_agent_channels(agent)
        if str(discovery.get("source", "")) == "permission":
            raise SetupError(str(discovery.get("detail", "live channel discovery requires root")))
        discovered = discovery.get("channels", [])
        if not isinstance(discovered, list) or not discovered:
            raise SetupError(str(discovery.get("detail", "no live channels discovered")))

        existing = agent.get("channels", [])
        existing_map: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(existing, list):
            for row in existing:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key[0] and key[1]:
                    existing_map[key] = dict(row)

        synced: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for channel in discovered:
            key = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if key in seen or not key[0] or not key[1]:
                continue
            seen.add(key)
            row = dict(existing_map.get(key, {}))
            row["kind"] = key[0]
            row["name"] = key[1]
            row["enabled"] = bool(channel.get("enabled", row.get("enabled", True)))
            row["external_id"] = str(row.get("external_id", f"{token}:{key[0]}:{len(synced) + 1}"))
            synced.append(row)

        if not replace and isinstance(existing, list):
            for row in existing:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key in seen or not key[0] or not key[1]:
                    continue
                synced.append(dict(row))

        agent["channels"] = synced
        agent.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "channels.synced_from_provider",
            f"Synced live channels for {token}",
            {
                "agent_id": token,
                "replace": bool(replace),
                "channel_count": len(synced),
                "discovered_provider": list(discovery.get("providers", [])),
            },
        )
        self.store.write_state(state)
        return self.get_dashboard_agent(token)

    def _mint_channels(
        self,
        agent_id: str,
        base_channels: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        items = list(base_channels or [])
        minted: list[dict[str, str]] = []
        for idx, channel in enumerate(items, start=1):
            kind = str(channel.get("kind", "chat"))
            raw_name = str(channel.get("name", f"channel-{idx}"))
            if raw_name.startswith(f"{agent_id}-"):
                full_name = raw_name
            else:
                full_name = f"{agent_id}-{raw_name}"
            minted.append(
                {
                    "kind": kind,
                    "name": full_name,
                    "external_id": f"{agent_id}:{kind}:{idx}",
                }
            )
        return minted

    @staticmethod
    def _find_channel(channels: list[dict[str, Any]], kind: str, name: str) -> int | None:
        for idx, channel in enumerate(channels):
            if not isinstance(channel, dict):
                continue
            row_kind = str(channel.get("kind", "")).strip().lower()
            row_name = str(channel.get("name", "")).strip()
            if row_kind == kind and row_name == name:
                return idx
        return None

    @staticmethod
    def _channel_key(kind: str, name: str) -> tuple[str, str]:
        return (str(kind).strip().lower(), str(name).strip())

    def _channel_keys(self, channels: list[dict[str, Any]]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            kind, name = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if not kind or not name:
                continue
            keys.add((kind, name))
        return keys

    def _assert_channels_unclaimed(
        self,
        agents: dict[str, Any],
        owner_agent_id: str,
        channels: list[dict[str, Any]],
        allow_owners: set[str] | None = None,
    ) -> None:
        keys = self._channel_keys(channels)
        if not keys:
            return
        allowed = {str(owner_agent_id).strip()}
        if allow_owners:
            for item in allow_owners:
                token = str(item).strip()
                if token:
                    allowed.add(token)
        conflicts: list[str] = []
        for aid, payload in sorted(agents.items()):
            token = str(aid).strip()
            if token in allowed:
                continue
            rows = payload.get("channels", [])
            if not isinstance(rows, list):
                continue
            claimed = [
                f"{kind}:{name}" for (kind, name) in keys if self._find_channel(rows, kind, name) is not None
            ]
            if claimed:
                conflicts.append(f"{token} owns {', '.join(claimed)}")
        if conflicts:
            raise ValueError("channel already assigned to another agent: " + "; ".join(conflicts))

    def _remove_channel_keys_from_agent(
        self,
        source: dict[str, Any],
        keys: set[tuple[str, str]],
    ) -> int:
        if not keys:
            return 0
        channels = source.setdefault("channels", [])
        if not isinstance(channels, list):
            source["channels"] = []
            return 0
        kept: list[Any] = []
        removed = 0
        for channel in channels:
            if not isinstance(channel, dict):
                kept.append(channel)
                continue
            kind, name = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if (kind, name) in keys:
                removed += 1
                continue
            kept.append(channel)
        source["channels"] = kept
        return removed

    def _remove_channel_from_other_agents(
        self,
        agents: dict[str, Any],
        kind: str,
        name: str,
        keep_agent_id: str,
    ) -> list[str]:
        keep = str(keep_agent_id).strip()
        moved_from: list[str] = []
        for aid, payload in agents.items():
            token = str(aid).strip()
            if token == keep:
                continue
            rows = payload.setdefault("channels", [])
            if not isinstance(rows, list):
                continue
            removed_any = False
            while True:
                found_idx = self._find_channel(rows, kind, name)
                if found_idx is None:
                    break
                rows.pop(found_idx)
                removed_any = True
            if removed_any:
                moved_from.append(token)
                payload.setdefault("agent", {})["last_sync"] = now_iso()
        return moved_from

    def _channel_connect_commands(
        self,
        provider: str,
        kind: str,
        name: str,
        linux_user: str,
    ) -> list[list[str]]:
        executable = self._resolve_provider_executable(provider)
        adapter = get_channel_adapter(provider)
        commands = adapter.connect_commands(executable=executable, kind=kind, name=name)

        wrapped: list[list[str]] = []
        for raw in commands:
            wrapped.append(self._wrap_user_command(raw, linux_user, purpose="channel connect"))
        return wrapped

    @staticmethod
    def _normalized_channel_pool(config: dict[str, Any]) -> list[dict[str, str]]:
        raw = config.get("channel_pool", [])
        if not isinstance(raw, list):
            return []
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kind": kind,
                    "name": name,
                    "provider": str(item.get("provider", "")).strip().lower(),
                    "external_id": str(item.get("external_id", "")).strip(),
                }
            )
        return rows

    def _read_channel_pool(self) -> list[dict[str, str]]:
        config = self.store.read_config()
        return self._normalized_channel_pool(config)

    def _write_channel_pool(self, channels: list[dict[str, str]]) -> None:
        config = self.store.read_config()
        config["channel_pool"] = self._normalized_channel_pool({"channel_pool": channels})
        config["updated_at"] = now_iso()
        self.store.write_config(config)

    def _remove_pool_channel(self, kind: str, name: str) -> None:
        current = self._read_channel_pool()
        remaining = [
            row
            for row in current
            if not (
                str(row.get("kind", "")).strip().lower() == kind
                and str(row.get("name", "")).strip() == name
            )
        ]
        if len(remaining) != len(current):
            self._write_channel_pool(remaining)

    def _local_channel_inventory(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for claw in self.list_installed_claws():
            provider = str(claw.get("provider", "")).strip().lower()
            root = Path(str(claw.get("root", "")).strip())
            if not provider or not root:
                continue
            discovered = self._discover_channels_for_provider_root(provider, root)
            for channel in discovered:
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name:
                    continue
                rows.append(
                    {
                        "source": "local",
                        "owner_agent_id": f"@local:{provider}",
                        "provider": provider,
                        "kind": kind,
                        "name": name,
                        "enabled": bool(channel.get("enabled", True)),
                    }
                )
        return rows

    def _discover_channels_for_provider_root(self, provider: str, root: Path) -> list[dict[str, str]]:
        adapter = get_channel_adapter(provider)
        return adapter.discover_channels(root)

    def _discover_agent_channels(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        linux_user = str(info.get("linux_user", "")).strip()
        is_local = bool(info.get("local_user", False))
        if is_local:
            home = self._local_agent_home(provider)
        else:
            home = self._agent_linux_home(payload)
        if not home:
            return {"source": "none", "detail": "agent home is not available", "channels": [], "providers": []}
        if linux_user and not is_local and not self._can_manage_linux_user(linux_user):
            if not self._can_read_provider_channel_roots(home, [provider, *provider_names()]):
                return {
                    "source": "permission",
                    "detail": "live channel discovery requires root for managed agents owned by another Linux user",
                    "channels": [],
                    "providers": [],
                }

        ordered: list[str] = []
        seen_providers: set[str] = set()
        candidate_providers: list[str] = []
        if linux_user and not is_local:
            candidate_providers.extend(self._live_provider_names_for_user(linux_user))
        if not candidate_providers:
            candidate_providers = [provider, *provider_names()]
        for item in candidate_providers:
            token = str(item or "").strip().lower()
            if not token or token in seen_providers:
                continue
            seen_providers.add(token)
            ordered.append(token)

        discovered: list[dict[str, Any]] = []
        found_providers: list[str] = []
        seen_channels: set[tuple[str, str]] = set()
        for name in ordered:
            root = home / get_provider(name).state_dir
            channels = self._discover_channels_for_provider_root(name, root)
            provider_had_rows = False
            for channel in channels:
                key = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
                if key in seen_channels or not key[0] or not key[1]:
                    continue
                seen_channels.add(key)
                provider_had_rows = True
                discovered.append(
                    {
                        "kind": key[0],
                        "name": key[1],
                        "enabled": bool(channel.get("enabled", True)),
                        "discovered_provider": name,
                    }
                )
            if provider_had_rows:
                found_providers.append(name)

        if discovered:
            return {
                "source": "provider",
                "detail": "live channels discovered",
                "channels": discovered,
                "providers": found_providers,
            }
        return {
            "source": "none",
            "detail": "no live channels discovered",
            "channels": [],
            "providers": [],
        }

    def _attach_agent_channel_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.setdefault("agent", {})
        stored = payload.get("channels", [])
        stored_rows = [dict(row) for row in stored if isinstance(row, dict)] if isinstance(stored, list) else []
        discovery = self._discover_agent_channels(payload)
        live_rows = discovery.get("channels", [])
        live_map = {
            self._channel_key(row.get("kind", ""), row.get("name", "")): dict(row)
            for row in live_rows
            if isinstance(row, dict)
        }

        merged: list[dict[str, Any]] = []
        appended: set[tuple[str, str]] = set()
        for row in stored_rows:
            key = self._channel_key(row.get("kind", ""), row.get("name", ""))
            if not key[0] or not key[1]:
                continue
            live = live_map.get(key)
            if live:
                row["channel_source"] = "live"
                row["discovered_provider"] = str(live.get("discovered_provider", ""))
            elif str(discovery.get("source", "")) == "provider":
                row["channel_source"] = "stale"
            else:
                row["channel_source"] = "state"
            merged.append(row)
            appended.add(key)

        for row in live_rows:
            if not isinstance(row, dict):
                continue
            key = self._channel_key(row.get("kind", ""), row.get("name", ""))
            if key in appended or not key[0] or not key[1]:
                continue
            merged.append(
                {
                    "kind": key[0],
                    "name": key[1],
                    "enabled": bool(row.get("enabled", True)),
                    "external_id": "",
                    "channel_source": "discovered",
                    "discovered_provider": str(row.get("discovered_provider", "")),
                }
            )
            appended.add(key)

        def _sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
            source = str(row.get("channel_source", "state"))
            order = {"live": 0, "discovered": 1, "state": 2, "stale": 3}
            return (order.get(source, 9), str(row.get("kind", "")), str(row.get("name", "")))

        payload["channels"] = sorted(merged, key=_sort_key)
        info["channel_status_source"] = str(discovery.get("source", "state"))
        info["channel_status_detail"] = str(discovery.get("detail", ""))
        info["live_channel_count"] = sum(
            1 for row in payload["channels"] if str(row.get("channel_source", "")) in {"live", "discovered"}
        )
        info["stale_channel_count"] = sum(
            1 for row in payload["channels"] if str(row.get("channel_source", "")) == "stale"
        )
        return payload

    def _discover_channels_from_source_home(
        self,
        source_home: Path,
        requested_provider: str | None,
    ) -> list[dict[str, str]]:
        providers: list[str] = []
        if requested_provider:
            providers.append(str(requested_provider).strip().lower())
        config = self.store.read_config()
        providers.append(str(config.get("provider", "openclaw")).strip().lower())

        channels: list[dict[str, str]] = []
        for provider in providers:
            try:
                state_dir = get_provider(provider).state_dir
            except ValueError:
                continue
            adapter = get_channel_adapter(provider)
            channels.extend(adapter.discover_channels(source_home / state_dir))
        return dedupe_channels(channels)

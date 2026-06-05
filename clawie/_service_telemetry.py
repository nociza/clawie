"""Dashboard payloads, metrics, and status attachment (ZeroClawService mixin)."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from clawie.service_common import now_iso


class TelemetryOpsMixin:

    def get_dashboard_agent(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            provider = token.split(":", 1)[1]
            payload = self._local_agent_view(provider)
        else:
            self._refresh_managed_agent_provider_alignment(token)
            payload = copy.deepcopy(self.get_agent(token))
        payload = self._attach_agent_runtime_status(payload)
        info = payload.setdefault("agent", {})
        info["status"] = self._dashboard_status(str(info.get("status", "")), info)
        payload = self._attach_agent_auth_status(payload)
        payload = self._attach_agent_addon_status(payload)
        return self._attach_agent_channel_view(payload)

    def dashboard_snapshot(self, agent_id: str | None = None) -> dict[str, Any]:
        return self.performance_snapshot(agent_id=agent_id, refresh=True)

    def performance_snapshot(
        self,
        agent_id: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if refresh:
            self.collect_metrics(agent_id=agent_id)
        daemon_map = self._running_provider_daemons_by_user()
        self._refresh_managed_agent_provider_alignments(agent_id=agent_id, daemon_map=daemon_map)
        state = self.store.read_state()
        agents = list(state.setdefault("agents", state.get("users", {})).values())
        if agent_id:
            agents = [
                row
                for row in agents
                if row.get("agent_id", row.get("user_id", "")) == agent_id
            ]
        latest_metrics = self.store.latest_metrics(limit_per_user=1)

        rows: list[dict[str, Any]] = []
        channel_total = 0
        migrated_total = 0
        cpu_total = 0.0
        mem_total = 0.0
        for agent_state in sorted(
            agents,
            key=lambda row: row.get("agent_id", row.get("user_id", "")),
        ):
            self._hydrate_agent_controls(agent_state)
            channel_view = self._attach_agent_channel_view(copy.deepcopy(agent_state))
            channel_view = self._attach_agent_runtime_status(channel_view, daemon_map=daemon_map)
            agent_info = channel_view.get("agent", {})
            channels = channel_view.get("channels", [])
            active_channels = sum(1 for channel in channels if bool(channel.get("enabled", True)))
            migrated_count = sum(1 for row in channels if row.get("migrated_from"))
            channel_total += len(channels)
            migrated_total += migrated_count
            current_id = str(agent_state.get("agent_id", agent_state.get("user_id", "")))
            metric = (latest_metrics.get(current_id, [{}]) or [{}])[0]
            cpu = float(metric.get("cpu_percent", 0.0))
            mem = float(metric.get("mem_percent", 0.0))
            rss = int(metric.get("rss_kb", 0))
            metric_status = str(metric.get("status", "")).strip()
            live_pid = int(agent_info.get("live_pid", 0) or 0)
            if live_pid > 0 and (metric_status in {"", "offline", "stopped", "unknown"} or rss <= 0):
                probe = self._probe_process(live_pid)
                if probe is not None:
                    cpu = float(probe["cpu_percent"])
                    mem = float(probe["mem_percent"])
                    rss = int(probe["rss_kb"])
                    metric_status = "running"
            sampled_status = self._dashboard_status(metric_status, agent_info)
            cpu_total += cpu
            mem_total += mem
            rows.append(
                {
                    "agent_id": current_id,
                    "display_name": agent_state.get("display_name", ""),
                    "status": sampled_status,
                    "version": agent_info.get("version", ""),
                    "provider": agent_info.get("provider", ""),
                    "provider_status": agent_info.get("provider_status", "ok"),
                    "provider_issue": agent_info.get("provider_issue", ""),
                    "provider_remediation": agent_info.get("provider_remediation", ""),
                    "strategy": agent_state.get("channel_strategy", ""),
                    "channels": active_channels,
                    "channels_total": len(channels),
                    "migrated": migrated_count,
                    "last_sync": agent_info.get("last_sync", ""),
                    "pid": live_pid or int(agent_info.get("pid") or 0),
                    "cpu_percent": cpu,
                    "mem_percent": mem,
                    "rss_kb": rss,
                }
            )

        for local_agent in self._local_dashboard_rows(refresh=refresh):
            if agent_id and local_agent["agent_id"] != agent_id:
                continue
            rows.append(local_agent)

        config = self.store.read_config()
        return {
            "generated_at": now_iso(),
            "workspace": config.get("workspace", ""),
            "provider": config.get("provider", "openclaw"),
            "totals": {
                "agents": len(rows),
                "channels": channel_total,
                "migrated_channels": migrated_total,
                "cpu_percent": round(cpu_total, 2),
                "mem_percent": round(mem_total, 2),
            },
            "rows": rows,
            "events": self.list_events(limit=8),
        }

    def collect_metrics(self, agent_id: str | None = None) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        sampled = 0
        for aid, agent_state in agents.items():
            if agent_id and aid != agent_id:
                continue
            agent = agent_state.setdefault("agent", {})
            pid = int(agent.get("pid") or 0)
            status = "offline"
            cpu_percent = 0.0
            mem_percent = 0.0
            rss_kb = 0
            if pid > 0:
                probe = self._probe_process(pid)
                if probe is not None:
                    cpu_percent = float(probe["cpu_percent"])
                    mem_percent = float(probe["mem_percent"])
                    rss_kb = int(probe["rss_kb"])
                    status = "running"
                else:
                    agent["pid"] = 0

            agent["status"] = status
            agent["last_sync"] = now_iso()
            self.store.write_metric(
                timestamp=now_iso(),
                user_id=aid,
                cpu_percent=cpu_percent,
                mem_percent=mem_percent,
                rss_kb=rss_kb,
                status=status,
            )
            sampled += 1
        self.store.write_state(state)
        return {"sampled": sampled}

    def _attach_agent_auth_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", payload.get("user_id", ""))).strip()
        info = payload.setdefault("agent", {})
        try:
            auth = self.agent_auth_status(agent_id)
        except Exception as exc:
            info["auth_status"] = "unknown"
            info["auth_profile"] = ""
            info["auth_account"] = ""
            info["auth_expires_at"] = ""
            info["auth_last_refresh"] = ""
            info["auth_source"] = "error"
            info["auth_detail"] = str(exc)
            info["login_required"] = False
            info["can_login"] = False
            return payload

        info["auth_mode"] = str(auth.get("auth_mode", info.get("auth_mode", "")))
        info["auth_status"] = str(auth.get("auth_status", "unknown"))
        info["auth_profile"] = str(auth.get("auth_profile", ""))
        info["auth_account"] = str(auth.get("account", ""))
        info["auth_expires_at"] = str(auth.get("expires_at", ""))
        info["auth_last_refresh"] = str(auth.get("last_refresh", ""))
        info["auth_source"] = str(auth.get("source", ""))
        info["auth_detail"] = str(auth.get("detail", ""))
        info["login_required"] = bool(auth.get("login_required", False))
        info["can_login"] = bool(auth.get("can_login", False))
        return payload

    def _attach_agent_addon_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", payload.get("user_id", ""))).strip()
        if not agent_id or agent_id.startswith("@local:"):
            payload["addon_access"] = {"agent_id": agent_id, "addons": []}
            return payload
        try:
            payload["addon_access"] = self.get_agent_addons(agent_id)
        except Exception:
            payload["addon_access"] = {"agent_id": agent_id, "addons": []}
        return payload

    def _attach_agent_runtime_status(
        self,
        payload: dict[str, Any],
        *,
        daemon_map: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        info = payload.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        info["provider_status"] = str(info.get("provider_status", "ok") or "ok")
        if bool(info.get("local_user", False)):
            info["live_provider"] = provider
            info["live_providers"] = [provider] if provider else []
            info["live_pid"] = int(info.get("fallback_pid", 0) or 0)
            return payload

        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            info["live_provider"] = ""
            info["live_providers"] = []
            info["live_pid"] = 0
            return payload

        if daemon_map is None:
            daemon_map = self._running_provider_daemons_by_user()
        live_entries = list(daemon_map.get(linux_user, []))
        live_providers: list[str] = []
        chosen_entry: dict[str, Any] | None = None
        reported_running = self._provider_reports_running(provider, linux_user) if provider else False
        for entry in live_entries:
            entry_provider = str(entry.get("provider", "")).strip().lower()
            if not entry_provider:
                continue
            if entry_provider not in live_providers:
                live_providers.append(entry_provider)
            if chosen_entry is None and (
                not str(info.get("provider", "")).strip().lower()
                or entry_provider == str(info.get("provider", "")).strip().lower()
            ):
                chosen_entry = entry
        if chosen_entry is None and live_entries:
            chosen_entry = live_entries[0]

        info["live_provider"] = str((chosen_entry or {}).get("provider", "")).strip().lower()
        info["live_providers"] = live_providers
        info["live_pid"] = int((chosen_entry or {}).get("pid", 0) or 0)
        info["live_command"] = str((chosen_entry or {}).get("args", ""))
        if live_entries:
            info["service_status"] = "running"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "process"
        elif reported_running is True:
            if provider:
                info["live_provider"] = provider
                info["live_providers"] = [provider]
            info["service_status"] = "running"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "systemd"
        elif reported_running is None:
            info["service_status"] = "unknown"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "systemd"
        else:
            info["service_status"] = "stopped"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "process"
        return payload

    def _local_dashboard_rows(self, refresh: bool = False) -> list[dict[str, Any]]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        installed = self.list_installed_claws()
        user_hints: dict[str, str] = {}
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            hint = self._linux_user_from_provider_root(Path(str(claw.get("root", "")).strip()))
            if hint:
                user_hints[provider] = hint
        providers = [
            str(row.get("provider", "")).strip().lower()
            for row in installed
            if str(row.get("provider", "")).strip()
        ]
        if refresh and providers:
            local_state = self._refresh_local_service_statuses(providers, local_state, user_hints=user_hints)
            config = self.store.read_config()
        rows: list[dict[str, Any]] = []
        # Use the same home-resolution logic as list_installed_claws() so
        # `sudo clawie status` still inspects the invoking user's claws.
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            local_info = local_state.get(provider, {})
            rows.append(
                {
                    "agent_id": f"@local:{provider}",
                    "display_name": "local-user",
                    "status": str(local_info.get("service_status", "unknown")),
                    "version": "local",
                    "provider": provider,
                    "strategy": "local-user",
                    "channels": 0,
                    "channels_total": 0,
                    "migrated": 0,
                    "last_sync": str(config.get("updated_at", "")),
                    "pid": int(local_info.get("fallback_pid", 0) or 0),
                    "cpu_percent": 0.0,
                    "mem_percent": 0.0,
                    "rss_kb": 0,
                    "local_user": True,
                }
            )
        return rows

    def _dashboard_status(self, metric_status: str, agent_info: dict[str, Any]) -> str:
        service_status = self._normalize_status_text(str(agent_info.get("service_status", "")))
        if service_status != "unknown":
            return service_status
        measured = self._normalize_status_text(metric_status)
        if measured != "unknown":
            return measured
        return self._normalize_status_text(str(agent_info.get("status", "unknown")))

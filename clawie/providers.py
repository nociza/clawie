from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    runtime: str
    auth_modes: tuple[str, ...]
    default_auth_mode: str
    default_api_url: str
    state_dir: str
    workspace_dir: str
    marker_files: tuple[str, ...]
    credential_paths: tuple[str, ...] = ()
    shared_auth_paths: tuple[str, ...] = ()
    core_prompt_files: tuple[str, ...] = ()
    install_method: str = ""
    install_package: str = ""
    service_group: str = "service"
    background_command: tuple[str, ...] = ("daemon",)
    auth_login_command: tuple[str, ...] = ("auth", "login")
    auth_refresh_command: tuple[str, ...] = ("auth", "refresh")
    auth_status_command: tuple[str, ...] = ("auth", "status")

    def supports_auth_mode(self, mode: str) -> bool:
        return mode in self.auth_modes


PROVIDERS: dict[str, ProviderSpec] = {
    "zeroclaw": ProviderSpec(
        name="zeroclaw",
        runtime="zeroclaw-agent",
        auth_modes=("linked", "api_key"),
        default_auth_mode="linked",
        default_api_url="https://api.zeroclaw.example/v1",
        state_dir=".zeroclaw",
        workspace_dir="workspace",
        marker_files=("config.toml", "auth-profiles.json", ".secret_key"),
        credential_paths=(
            ".zeroclaw",
            ".config/zeroclaw",
            ".codex",
            ".config/openai",
            ".openai",
        ),
        shared_auth_paths=(
            ".zeroclaw/auth-profiles.json",
        ),
        core_prompt_files=(
            "SOUL.md",
            "IDENTITY.md",
            "AGENTS.md",
            "TOOLS.md",
            "MEMORY.md",
            "HEARTBEAT.md",
            "BOOTSTRAP.md",
            "USER.md",
        ),
        install_method="brew",
        install_package="zeroclaw",
        service_group="service",
        background_command=("daemon",),
    ),
    "picoclaw": ProviderSpec(
        name="picoclaw",
        runtime="picoclaw-agent",
        auth_modes=("linked", "api_key"),
        default_auth_mode="linked",
        default_api_url="https://api.picoclaw.example/v1",
        state_dir=".picoclaw",
        workspace_dir="workspace",
        marker_files=("config.json", "config.toml", "auth.json", "auth-profiles.json"),
        credential_paths=(
            ".picoclaw",
            ".config/picoclaw",
            ".codex",
            ".config/openai",
            ".openai",
        ),
        shared_auth_paths=(
            ".picoclaw/auth.json",
            ".picoclaw/auth-profiles.json",
        ),
        core_prompt_files=(
            "SOUL.md",
            "IDENTITY.md",
            "AGENTS.md",
            "TOOLS.md",
            "MEMORY.md",
            "HEARTBEAT.md",
            "BOOTSTRAP.md",
            "USER.md",
        ),
        install_method="brew",
        install_package="picoclaw",
        service_group="",
        background_command=("gateway",),
        auth_login_command=("auth", "login", "--provider", "openai"),
        auth_refresh_command=("auth", "status"),
        auth_status_command=("auth", "status"),
    ),
    "openclaw": ProviderSpec(
        name="openclaw",
        runtime="openclaw-agent",
        auth_modes=("none", "linked", "api_key"),
        default_auth_mode="none",
        default_api_url="https://api.openclaw.example/v1",
        state_dir=".openclaw",
        workspace_dir="workspace",
        marker_files=(
            "openclaw.json",
            "auth-profiles.json",
            "agents/*/agent/auth-profiles.json",
        ),
        credential_paths=(
            ".openclaw",
            ".config/openclaw",
            ".codex",
            ".config/openai",
            ".openai",
        ),
        shared_auth_paths=(
            ".openclaw/auth-profiles.json",
        ),
        core_prompt_files=(
            "SOUL.md",
            "IDENTITY.md",
            "AGENTS.md",
            "TOOLS.md",
            "MEMORY.md",
            "HEARTBEAT.md",
            "BOOTSTRAP.md",
            "USER.md",
        ),
        install_method="pnpm",
        install_package="openclaw",
        service_group="daemon",
        background_command=("gateway", "run"),
    ),
}


def provider_names() -> list[str]:
    return sorted(PROVIDERS)


def get_provider(name: str) -> ProviderSpec:
    normalized = (name or "").strip().lower()
    if normalized not in PROVIDERS:
        choices = ", ".join(provider_names())
        raise ValueError(f"provider must be one of: {choices}")
    return PROVIDERS[normalized]


def credential_paths_for_providers(names: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        spec = get_provider(name)
        for rel in spec.credential_paths:
            if rel in seen:
                continue
            seen.add(rel)
            ordered.append(rel)
    return ordered


def shared_auth_paths_for_providers(names: list[str]) -> list[str]:
    ordered = [".codex/auth.json"]
    seen = {".codex/auth.json"}
    for name in names:
        spec = get_provider(name)
        for rel in spec.shared_auth_paths:
            if rel in seen:
                continue
            seen.add(rel)
            ordered.append(rel)
    return ordered


def detect_installed_providers(home_dir: str) -> list[dict[str, object]]:
    from pathlib import Path

    source = Path(home_dir).expanduser()
    findings: list[dict[str, object]] = []
    for name in provider_names():
        spec = get_provider(name)
        root = source / spec.state_dir
        try:
            exists = root.exists()
        except OSError:
            continue
        if not exists:
            continue
        markers = []
        for marker in spec.marker_files:
            rel = f"{spec.state_dir}/{marker}"
            if "*" in marker:
                try:
                    matched = list(source.glob(rel))
                except OSError:
                    matched = []
                for item in matched[:3]:
                    try:
                        markers.append(str(item.relative_to(source)))
                    except OSError:
                        continue
            else:
                try:
                    marker_exists = (source / rel).exists()
                except OSError:
                    marker_exists = False
                if not marker_exists:
                    continue
                markers.append(rel)
        if not markers:
            continue
        findings.append(
            {
                "provider": spec.name,
                "root": str(root),
                "markers": markers,
            }
        )
    return findings

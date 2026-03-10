from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AddonSpec:
    name: str
    label: str
    description: str
    executable: str
    install_method: str
    install_package: str
    shared_config_dir: str
    target_config_rel: str
    auth_files: tuple[str, ...]
    auth_status_command: tuple[str, ...] = ()
    auth_login_command: tuple[str, ...] = ()
    auth_setup_command: tuple[str, ...] = ()
    auth_export_command: tuple[str, ...] = ()
    config_dir_env: str = ""


ADDONS: dict[str, AddonSpec] = {
    "gws": AddonSpec(
        name="gws",
        label="Google Workspace CLI",
        description="Google Workspace API CLI for Gmail, Drive, Sheets, Chat, and more",
        executable="gws",
        install_method="npm",
        install_package="@googleworkspace/cli",
        shared_config_dir="gws",
        target_config_rel=".config/gws",
        auth_files=("credentials.json", "client_secret.json"),
        auth_status_command=("auth", "status"),
        auth_login_command=("auth", "login"),
        auth_setup_command=("auth", "setup"),
        auth_export_command=("auth", "export", "--unmasked"),
        config_dir_env="GOOGLE_WORKSPACE_CLI_CONFIG_DIR",
    ),
}


def addon_names() -> list[str]:
    return sorted(ADDONS)


def get_addon(name: str) -> AddonSpec:
    token = str(name or "").strip().lower()
    if token not in ADDONS:
        choices = ", ".join(addon_names())
        raise ValueError(f"addon must be one of: {choices}")
    return ADDONS[token]

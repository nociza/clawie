from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"


def test_install_script_has_valid_shell_syntax() -> None:
    result = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_install_script_refuses_unsupported_os(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "uname", "#!/usr/bin/env sh\necho Darwin\n")
    _write_executable(fake_bin / "uv", "#!/usr/bin/env sh\nexit 99\n")

    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run([str(INSTALL)], capture_output=True, text=True, check=False, env=env)

    assert result.returncode == 1
    assert "clawie is Linux-only" in result.stderr


def test_install_script_allows_development_override_and_python_version(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    tool_dir = tmp_path / "tools"
    clawie_bin = tool_dir / "clawie" / "bin" / "clawie"
    clawie_bin.parent.mkdir(parents=True)
    clawie_bin.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    _write_executable(fake_bin / "uname", "#!/usr/bin/env sh\necho Darwin\n")
    _write_executable(
        fake_bin / "uv",
        "#!/usr/bin/env sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        f'  echo "{tool_dir}"\n'
        "fi\n",
    )

    env = {
        **os.environ,
        "CLAWIE_ALLOW_UNSUPPORTED_OS": "1",
        "PYTHON_VERSION": "3.11",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run([str(INSTALL)], capture_output=True, text=True, check=False, env=env)

    assert result.returncode == 0, result.stderr
    assert "Installed:" in result.stdout
    logged = log.read_text(encoding="utf-8")
    assert f"tool install --force -e {ROOT} --python 3.11" in logged


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)

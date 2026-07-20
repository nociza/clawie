#!/usr/bin/env bash
# Install or update the clawie CLI from this repository.
#
# Usage:
#   ./install.sh          # install for current user
#   sudo ./install.sh     # install system-wide shim + for root
#
# Optional environment:
#   PYTHON_VERSION=3.11              # choose uv-managed Python (default: 3.12)
#   CLAWIE_ALLOW_UNSUPPORTED_OS=1    # development-only bypass for non-Linux
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

OS_NAME="$(uname -s)"
if [ "${OS_NAME}" != "Linux" ] && [ "${CLAWIE_ALLOW_UNSUPPORTED_OS:-}" != "1" ]; then
    cat >&2 <<EOF
Error: clawie is Linux-only. It relies on Linux users, systemd, and Unix sockets.
Set CLAWIE_ALLOW_UNSUPPORTED_OS=1 only for development tasks that do not use
runtime isolation, watchdogs, or production verification.
EOF
    exit 1
fi

# Ensure uv is available
if ! command -v uv &>/dev/null; then
    echo "Error: uv is required. Install it from https://docs.astral.sh/uv/" >&2
    exit 1
fi

echo "Installing clawie from ${REPO_DIR} ..."
# Production installs must copy a built artifact into the tool environment.
# An editable root install would continue importing code from the checkout.
uv tool install --force "${REPO_DIR}" --python "${PYTHON_VERSION}"

# Find where uv put the binary
CLAWIE_BIN="$(uv tool dir)/clawie/bin/clawie"
if [ ! -f "${CLAWIE_BIN}" ]; then
    CLAWIE_BIN="$(dirname "$(uv tool run --from clawie which clawie 2>/dev/null || true)")/clawie"
fi

echo "Installed: ${CLAWIE_BIN}"

# If running as root, also create/update the system-wide shim
if [ "$(id -u)" -eq 0 ]; then
    SHIM="/usr/local/bin/clawie"
    mkdir -p "$(dirname "${SHIM}")"
    ln -sfn "${CLAWIE_BIN}" "${SHIM}"
    echo "System shim updated: ${SHIM} -> ${CLAWIE_BIN}"
fi

echo "Done. Run 'clawie --help' to verify."

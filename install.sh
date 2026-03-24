#!/usr/bin/env bash
# Install or update the clawie CLI from this repository.
#
# Usage:
#   ./install.sh          # install for current user
#   sudo ./install.sh     # install system-wide shim + for root
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_VERSION="3.12"

# Ensure uv is available
if ! command -v uv &>/dev/null; then
    echo "Error: uv is required. Install it from https://docs.astral.sh/uv/" >&2
    exit 1
fi

echo "Installing clawie from ${REPO_DIR} ..."
uv tool install --force -e "${REPO_DIR}" --python "${PYTHON_VERSION}"

# Find where uv put the binary
CLAWIE_BIN="$(uv tool dir)/clawie/bin/clawie"
if [ ! -f "${CLAWIE_BIN}" ]; then
    CLAWIE_BIN="$(dirname "$(uv tool run --from clawie which clawie 2>/dev/null || true)")/clawie"
fi

echo "Installed: ${CLAWIE_BIN}"

# If running as root, also create/update the system-wide shim
if [ "$(id -u)" -eq 0 ]; then
    SHIM="/usr/local/bin/clawie"
    cat > "${SHIM}" <<SHIM_EOF
#!/usr/bin/env bash
set -euo pipefail
exec ${CLAWIE_BIN} "\$@"
SHIM_EOF
    chmod 755 "${SHIM}"
    echo "System shim updated: ${SHIM} -> ${CLAWIE_BIN}"
fi

echo "Done. Run 'clawie --help' to verify."

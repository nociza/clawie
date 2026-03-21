"""Generic addon integration: TOOLS.md snippet and shell env block injection/removal."""

from __future__ import annotations

import re


# ── TOOLS.md snippet injection ────────────────────────────────────────

def _tools_begin_marker(addon_name: str) -> str:
    return f"<!-- clawie-{addon_name}-tools-begin -->"


def _tools_end_marker(addon_name: str) -> str:
    return f"<!-- clawie-{addon_name}-tools-end -->"


def inject_addon_tools_snippet(content: str, addon_name: str, snippet_body: str) -> str:
    """Wrap *snippet_body* with begin/end markers and append-or-replace in *content*."""
    begin = _tools_begin_marker(addon_name)
    end = _tools_end_marker(addon_name)
    block = f"{begin}\n{snippet_body}\n{end}"
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    if pattern.search(content):
        return pattern.sub(block, content)
    sep = "\n\n" if content.strip() else ""
    return content.rstrip() + sep + block + "\n"


def remove_addon_tools_snippet(content: str, addon_name: str) -> str:
    """Remove the marked tools block for *addon_name* from *content*."""
    begin = _tools_begin_marker(addon_name)
    end = _tools_end_marker(addon_name)
    pattern = re.compile(
        r"\n*" + re.escape(begin) + r".*?" + re.escape(end) + r"\n*",
        re.DOTALL,
    )
    return pattern.sub("\n", content).rstrip() + "\n" if content.strip() else ""


# ── Shell env block injection ─────────────────────────────────────────

def _env_begin_marker(addon_name: str) -> str:
    return f"# >>> clawie-{addon_name} >>>"


def _env_end_marker(addon_name: str) -> str:
    return f"# <<< clawie-{addon_name} <<<"


def render_addon_env_block(addon_name: str, exports_dict: dict[str, str]) -> str:
    """Render a shell block with ``export VAR=val`` lines wrapped in markers."""
    lines = [_env_begin_marker(addon_name)]
    for var, val in exports_dict.items():
        lines.append(f"export {var}={val}")
    lines.append(_env_end_marker(addon_name))
    lines.append("")  # trailing newline
    return "\n".join(lines)


def inject_addon_env_block(content: str, addon_name: str, block: str) -> str:
    """Append or replace the env block for *addon_name* in shell profile *content*."""
    begin = _env_begin_marker(addon_name)
    end = _env_end_marker(addon_name)
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    if pattern.search(content):
        return pattern.sub(block.rstrip(), content)
    sep = "\n" if content.strip() and not content.endswith("\n") else ""
    return content + sep + block


def remove_addon_env_block(content: str, addon_name: str) -> str:
    """Remove the env block for *addon_name* from shell profile *content*."""
    begin = _env_begin_marker(addon_name)
    end = _env_end_marker(addon_name)
    pattern = re.compile(
        r"\n*" + re.escape(begin) + r".*?" + re.escape(end) + r"\n*",
        re.DOTALL,
    )
    return pattern.sub("\n", content)

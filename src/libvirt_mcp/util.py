"""Small shared helpers: state names, safe subprocess wrapper, formatting."""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from libvirt_mcp import compat

libvirt = compat.libvirt

log = logging.getLogger("libvirt_mcp")

# How much of a command's output we keep in tool results.
OUTPUT_LIMIT = 100_000


class LibvirtMcpError(Exception):
    """Operational error whose message is safe and useful for the MCP client."""


DOMAIN_STATES = {
    0: "nostate",
    1: "running",
    2: "blocked",
    3: "paused",
    4: "shutdown",
    5: "shut off",
    6: "crashed",
    7: "pmsuspended",
}


def state_name(code: int) -> str:
    return DOMAIN_STATES.get(code, f"unknown({code})")


def domain_state(dom) -> str:
    return state_name(dom.state()[0])


def tail(text: str, limit: int = 2000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"...(truncated)...\n{text[-limit:]}"


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(output truncated at {limit} characters)"


def human_bytes(size) -> str:
    try:
        size = float(size)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def libvirt_version_string(version: int) -> str:
    """Format a libvirt version integer (e.g. 12000000 -> '12.0.0')."""

    return f"{version // 1_000_000}.{(version // 1000) % 1000}.{version % 1000}"


def run_cmd(args, *, timeout: int | None = None, env=None, what: str | None = None):
    """Run a command, raising LibvirtMcpError with useful context on failure."""

    cmd = [str(a) for a in args]
    label = what or shlex.join(cmd)
    log.debug("running: %s", shlex.join(cmd))
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as e:
        raise LibvirtMcpError(f"{label} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise LibvirtMcpError(
            f"required program not found: {e.filename} "
            "(install the package that provides it — see the README)"
        ) from e
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        raise LibvirtMcpError(
            f"{label} failed with exit code {cp.returncode}: {tail(detail, 1500)}"
        )
    return cp


def require_ssh_keypair(key_path) -> Path:
    """Fatal check that the dedicated SSH keypair exists. Returns the .pub path."""

    key = Path(key_path).expanduser()
    pub = Path(str(key) + ".pub")
    if not (key.is_file() and pub.is_file()):
        raise LibvirtMcpError(
            f"SSH keypair not found at {key} / {pub}.\n"
            f"The libvirt-mcp SSH key is injected into VMs and used as the SSH "
            f"fallback for exec_in_vm; it must exist before VMs are created. "
            f"Generate it with:\n"
            f"  ssh-keygen -t ed25519 -N '' -f {key}"
        )
    return pub

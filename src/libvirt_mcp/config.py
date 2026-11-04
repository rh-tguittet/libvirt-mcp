"""Runtime configuration, read from environment variables.

Defaults assume a single-user workstation using ``qemu:///system`` with disk
images in ``~/VirtualMachines`` and the dedicated SSH keypair ``~/.ssh/vms``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Config:
    uri: str
    images_dir: Path
    ssh_key: Path
    ssh_port: int
    customize_timeout: int
    exec_timeout: int


_cached: Config | None = None


def get_config() -> Config:
    global _cached
    if _cached is None:
        _cached = Config(
            uri=_env("LIBVIRT_MCP_URI", "qemu:///system"),
            images_dir=Path(
                _env("LIBVIRT_MCP_IMAGES_DIR", str(Path.home() / "VirtualMachines"))
            ).expanduser(),
            ssh_key=Path(
                _env("LIBVIRT_MCP_SSH_KEY", str(Path.home() / ".ssh" / "vms"))
            ).expanduser(),
            ssh_port=int(_env("LIBVIRT_MCP_SSH_PORT", "22")),
            customize_timeout=int(_env("LIBVIRT_MCP_CUSTOMIZE_TIMEOUT", "1800")),
            exec_timeout=int(_env("LIBVIRT_MCP_EXEC_TIMEOUT", "120")),
        )
    return _cached


def reset_config() -> None:
    global _cached
    _cached = None

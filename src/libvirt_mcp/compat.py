"""Import the libvirt python bindings.

The bindings are ABI-coupled to the system libvirt, so the recommended source
is the distro package (``python3-libvirt``). When the server runs inside a
virtualenv that does not expose the system site-packages, this module falls
back to adding the system site-packages to ``sys.path`` so the distro-provided
bindings are found anyway.
"""

from __future__ import annotations

import sys
from pathlib import Path

libvirt = None
libvirt_qemu = None


def _system_site_packages() -> list[Path]:
    """Return candidate system site-packages directories (distro layouts)."""

    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = []
    for base in ("/usr/lib64", "/usr/lib", "/usr/local/lib64", "/usr/local/lib"):
        path = Path(base) / ver / "site-packages"
        if path.is_dir():
            candidates.append(path)
    return candidates


def _import_bindings() -> None:
    global libvirt, libvirt_qemu
    import importlib

    libvirt = importlib.import_module("libvirt")
    try:
        libvirt_qemu = importlib.import_module("libvirt_qemu")
    except ImportError:
        libvirt_qemu = None


def _load() -> None:
    try:
        _import_bindings()
        return
    except ImportError:
        pass

    # Fall back to the system site-packages (python3-libvirt from the distro).
    for path in _system_site_packages():
        sys.path.insert(0, str(path))
        try:
            _import_bindings()
            return
        except ImportError:
            sys.path.remove(str(path))

    raise ImportError(
        "The libvirt python bindings were not found. Install them with your "
        "package manager (python3-libvirt on Fedora/RHEL/Debian/Ubuntu) and "
        "create the virtualenv with `uv venv --system-site-packages`. "
        "See the README for details."
    )


_load()

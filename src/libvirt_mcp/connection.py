"""Shared, cached connection to libvirt plus domain lookup helpers."""

from __future__ import annotations

import threading

from libvirt_mcp import compat
from libvirt_mcp.config import get_config
from libvirt_mcp.util import LibvirtMcpError

libvirt = compat.libvirt

_lock = threading.Lock()
_conn = None


def _quiet_libvirt_error_log(ctxt, err):
    """Swallow libvirt's default stderr prints.

    Expected lookups (e.g. checking whether a VM name is free) still raise
    proper python exceptions; this only stops the duplicate C-level printing
    that pollutes the server's stderr.
    """


try:
    libvirt.registerErrorHandler(_quiet_libvirt_error_log, None)
except Exception:  # pragma: no cover - very old bindings
    pass


def get_conn():
    """Return the shared RW connection, reopening it if it went away."""

    global _conn
    with _lock:
        if _conn is not None:
            try:
                if _conn.isAlive():
                    return _conn
            except libvirt.libvirtError:
                pass
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        uri = get_config().uri
        try:
            _conn = libvirt.open(uri)
        except libvirt.libvirtError as e:
            raise LibvirtMcpError(
                f"cannot connect to libvirt ({uri}): {e} "
                "Is libvirtd running and are you a member of the 'libvirt' group?"
            ) from e
        return _conn


def reset_conn() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None


def lookup_domain(name: str):
    """Look up a domain by name with a friendly error when it does not exist."""

    conn = get_conn()
    try:
        return conn.lookupByName(name)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            raise LibvirtMcpError(
                f"VM '{name}' not found. Use list_vms to see available VMs."
            ) from None
        raise

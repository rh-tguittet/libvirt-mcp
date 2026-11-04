"""virtiofs host <-> VM file shares (shared memory + <filesystem> device).

Adding a share rewrites the persistent domain XML with:
  * <memoryBacking><source type='memfd'/><access mode='shared'/></memoryBacking>
    (required by the vhost-user connection with the virtiofsd daemon)
  * a <filesystem type='mount' model='virtiofs'> device

libvirt (>= 7.2) starts virtiofsd automatically for such devices.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from libvirt_mcp import xmlutil
from libvirt_mcp.connection import get_conn, lookup_domain
from libvirt_mcp.util import LibvirtMcpError, state_name

libvirt = None


def _lv():
    global libvirt
    if libvirt is None:
        from libvirt_mcp import compat

        libvirt = compat.libvirt
    return libvirt


VIRTIOFSD_CANDIDATES = (
    "virtiofsd",
    "/usr/libexec/virtiofsd",
    "/usr/lib/qemu/virtiofsd",
    "/usr/lib/x86_64-linux-gnu/qemu/virtiofsd",
    "/usr/lib64/qemu/virtiofsd",
)


def virtiofsd_available() -> bool:
    return any(shutil.which(p) or Path(p).is_file() for p in VIRTIOFSD_CANDIDATES)


def _require_shutoff(dom, action: str) -> None:
    state = dom.state()[0]
    if state not in (_lv().VIR_DOMAIN_SHUTOFF, _lv().VIR_DOMAIN_CRASHED):
        raise LibvirtMcpError(
            f"VM '{dom.name()}' is {state_name(state)}; {action} requires it to be "
            "shut off (shared-memory backing cannot be changed at runtime)"
        )


def _sanitize_tag(path: Path) -> str:
    tag = "".join(c if c.isalnum() or c in "._-" else "-" for c in path.name)
    return tag or "share"


def add_fileshare(
    name: str,
    host_path: str,
    mount_tag: str | None = None,
    readonly: bool = False,
    accessmode: str = "passthrough",
) -> dict:
    dom = lookup_domain(name)
    _require_shutoff(dom, "adding a virtiofs share")

    host_dir = Path(host_path).expanduser()
    created_dir = False
    if not host_dir.exists():
        host_dir.mkdir(parents=True)
        created_dir = True
    host_dir = host_dir.resolve()

    tag = mount_tag or _sanitize_tag(host_dir)
    try:
        xmlutil.validate_mount_tag(tag)
    except ValueError as e:
        raise LibvirtMcpError(str(e)) from None

    tree = xmlutil.parse(dom.XMLDesc(0))
    root = tree.getroot()

    try:
        xmlutil.add_virtiofs_device(root, str(host_dir), tag, accessmode, readonly)
    except ValueError as e:
        raise LibvirtMcpError(f"cannot add share to VM '{name}': {e}") from None
    memory_change = xmlutil.ensure_shared_memory_backing(root)

    get_conn().defineXML(xmlutil.to_str(tree))

    result = {
        "name": name,
        "host_path": str(host_dir),
        "mount_tag": tag,
        "readonly": readonly,
        "accessmode": accessmode,
        "host_path_created": created_dir,
        "memory_backing": memory_change,
        "mount_in_guest": f"mkdir -p /mnt/{tag} && mount -t virtiofs {tag} /mnt/{tag}",
        "note": (
            "takes effect on next boot (start_vm); the guest kernel needs virtiofs "
            "support (Linux >= 5.4). To run commands like this, use exec_in_vm."
        ),
    }
    if not virtiofsd_available():
        result["warning"] = (
            "virtiofsd was not found in the usual locations; the VM may fail to start. "
            "Install it (dnf install virtiofsd / apt install virtiofsd)."
        )
    return result


def remove_fileshare(name: str, mount_tag: str, keep_memory_backing: bool = False) -> dict:
    dom = lookup_domain(name)
    _require_shutoff(dom, "removing a virtiofs share")

    tree = xmlutil.parse(dom.XMLDesc(0))
    root = tree.getroot()

    if not xmlutil.remove_virtiofs_device(root, mount_tag):
        raise LibvirtMcpError(
            f"no file share with mount tag '{mount_tag}' in VM '{name}'; "
            "use list_fileshares to see configured shares"
        )

    removed_backing = False
    if not keep_memory_backing and not xmlutil.filesystems(root):
        removed_backing = xmlutil.strip_shared_memory_backing(root)

    get_conn().defineXML(xmlutil.to_str(tree))
    return {
        "name": name,
        "removed_mount_tag": mount_tag,
        "memory_backing_removed": removed_backing,
        "note": "host directory was not deleted; only the share was removed",
    }


def list_fileshares(name: str) -> dict:
    dom = lookup_domain(name)
    root = xmlutil.parse(dom.XMLDesc(0)).getroot()
    shares = xmlutil.filesystems(root)
    for share in shares:
        if share["mount_tag"]:
            share["mount_in_guest"] = f"mount -t virtiofs {share['mount_tag']} /mnt/{share['mount_tag']}"
    return {
        "name": name,
        "shares": shares,
        "memory_backing": xmlutil.memory_backing_summary(root),
    }

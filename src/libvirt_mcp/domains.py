"""VM inventory, lifecycle, cloning and deletion.

Everything in here talks to libvirt through the official python bindings.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from libvirt_mcp import compat, guest, images, xmlutil
from libvirt_mcp.config import get_config
from libvirt_mcp.connection import get_conn, lookup_domain
from libvirt_mcp.util import (
    LibvirtMcpError,
    domain_state,
    human_bytes,
    state_name,
)

libvirt = compat.libvirt

_RUNNING = libvirt.VIR_DOMAIN_RUNNING
_PAUSED = libvirt.VIR_DOMAIN_PAUSED
_SHUTOFF = libvirt.VIR_DOMAIN_SHUTOFF
_CRASHED = libvirt.VIR_DOMAIN_CRASHED
_PMSUSPENDED = libvirt.VIR_DOMAIN_PMSUSPENDED


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------


def _ipv4_from_source(dom, source: int) -> list[str]:
    try:
        ifaces = dom.interfaceAddresses(source)
    except libvirt.libvirtError:
        return []
    ips: list[str] = []
    for name, data in (ifaces or {}).items():
        if name == "lo":
            continue
        for addr in data.get("addrs", []):
            if addr.get("type") == libvirt.VIR_IP_ADDR_TYPE_IPV4 and not addr["addr"].startswith("127."):
                ips.append(addr["addr"])
    return sorted(set(ips))


def best_effort_ips(dom) -> list[str]:
    """Cheap IP lookup (DHCP leases, then ARP cache); no guest agent involved."""

    return _ipv4_from_source(dom, libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE) or _ipv4_from_source(
        dom, libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP
    )


def list_vms() -> list[dict]:
    conn = get_conn()
    vms = []
    for dom in conn.listAllDomains(0):
        info = dom.info()  # [state, maxMem KiB, memory KiB, vcpus, cpuTime]
        state = info[0]
        vms.append(
            {
                "name": dom.name(),
                "id": dom.ID(),
                "state": state_name(state),
                "autostart": bool(dom.autostart()),
                "memory_mb": info[1] // 1024,
                "vcpus": info[3],
                "ipv4": best_effort_ips(dom) if state == _RUNNING else [],
            }
        )
    return sorted(vms, key=lambda vm: vm["name"])


def get_vm_info(name: str, include_xml: bool = False) -> dict:
    dom = lookup_domain(name)
    info = dom.info()
    tree = xmlutil.parse(dom.XMLDesc(0))
    root = tree.getroot()

    disks = []
    for entry in xmlutil.disk_entries(root):
        disk = {
            "dev": entry["dev"],
            "bus": entry["bus"],
            "device": entry["device"],
            "type": entry["type"],
            "path": entry["path"],
            "format": entry["driver_type"],
            "readonly": entry["readonly"],
        }
        if entry["path"] and entry["device"] == "disk" and os.path.isfile(entry["path"]):
            try:
                qi = images.qemu_img_info(entry["path"])
                disk["format"] = disk["format"] or qi.get("format")
                disk["virtual_size"] = human_bytes(qi.get("virtual-size"))
                disk["size_on_disk"] = human_bytes(qi.get("actual-size"))
            except LibvirtMcpError:
                pass
        disks.append(disk)

    running = info[0] == _RUNNING
    agent_configured = xmlutil.has_agent_channel(root)
    agent_reachable = bool(running and agent_configured and guest.agent_ping(dom))
    ifaces = guest.interface_addresses(dom) if running else []
    ipv4 = sorted({a for i in ifaces for a in i["ipv4"]})

    try:
        snapshots = [s.getName() for s in dom.listAllSnapshots(0)]
    except libvirt.libvirtError:
        snapshots = []
    try:
        current_snapshot = dom.snapshotCurrent().getName() if snapshots else None
    except libvirt.libvirtError:
        current_snapshot = None
    except AttributeError:
        current_snapshot = None

    out = {
        "name": dom.name(),
        "uuid": dom.UUIDString(),
        "id": dom.ID(),
        "state": state_name(info[0]),
        "autostart": bool(dom.autostart()),
        "persistent": bool(dom.isPersistent()),
        "memory_mb": info[1] // 1024,
        "memory_in_use_mb": info[2] // 1024,
        "vcpus": info[3],
        "cpu_time_s": round(info[4] / 1e9, 1),
        "os": xmlutil.os_info(root),
        "cpu_mode": root.find("cpu").get("mode") if root.find("cpu") is not None else None,
        "disks": disks,
        "interfaces": xmlutil.interfaces_xml(root),
        "ipv4": ipv4,
        "guest_agent": {
            "channel_configured": agent_configured,
            "reachable": agent_reachable,
        },
        "fileshares": xmlutil.filesystems(root),
        "memory_backing": xmlutil.memory_backing_summary(root),
        "snapshots": snapshots,
        "current_snapshot": current_snapshot,
        "managed_save_pending": bool(dom.hasManagedSaveImage()),
    }
    if include_xml:
        out["xml"] = dom.XMLDesc(0)
    return out


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def start_vm(name: str) -> dict:
    dom = lookup_domain(name)
    state = dom.state()[0]
    if state == _RUNNING:
        return {"name": name, "state": "running", "note": "already running"}
    if state == _PAUSED:
        dom.resume()
        return {"name": name, "state": domain_state(dom), "note": "resumed from paused"}
    if state == _PMSUSPENDED:
        raise LibvirtMcpError(
            f"VM '{name}' is suspended to RAM (pmsuspended) and the python bindings "
            "do not expose dompmwakeup; wake it with `virsh dompmwakeup` first"
        )
    dom.create()
    return {"name": name, "state": domain_state(dom)}


def _wait_shutoff(dom, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dom.state()[0] == _SHUTOFF:
            return True
        time.sleep(2)
    return dom.state()[0] == _SHUTOFF


def stop_vm(name: str, force: bool = False, wait: bool = False, wait_timeout: int = 120) -> dict:
    dom = lookup_domain(name)
    state = dom.state()[0]
    if state == _SHUTOFF:
        return {"name": name, "state": "shut off", "note": "already shut off"}
    if state == _PMSUSPENDED:
        raise LibvirtMcpError(
            f"VM '{name}' is suspended to RAM; wake it with `virsh dompmwakeup` before stopping it"
        )

    if force:
        dom.destroy()  # pull the power plug
        return {"name": name, "state": domain_state(dom), "force": True}

    dom.shutdown()  # graceful ACPI shutdown
    if wait and not _wait_shutoff(dom, wait_timeout):
        raise LibvirtMcpError(
            f"VM '{name}' did not power off within {wait_timeout}s (the guest may ignore ACPI). "
            "Retry with force=true (and confirm=true) to cut power."
        )
    return {"name": name, "state": domain_state(dom), "force": False}


def reboot_vm(name: str, force: bool = False) -> dict:
    dom = lookup_domain(name)
    state = dom.state()[0]
    if state not in (_RUNNING, _PAUSED, libvirt.VIR_DOMAIN_BLOCKED):
        raise LibvirtMcpError(
            f"VM '{name}' is {state_name(state)}; reboot requires it to be running"
        )
    if force:
        dom.reset()  # reset button
    else:
        dom.reboot(0)  # graceful ACPI reboot
    return {"name": name, "state": domain_state(dom), "force": force}


def pause_vm(name: str) -> dict:
    dom = lookup_domain(name)
    if dom.state()[0] != _RUNNING:
        raise LibvirtMcpError(f"VM '{name}' is {domain_state(dom)}; pause requires it to be running")
    dom.suspend()
    return {"name": name, "state": domain_state(dom)}


def resume_vm(name: str) -> dict:
    dom = lookup_domain(name)
    if dom.state()[0] != _PAUSED:
        raise LibvirtMcpError(f"VM '{name}' is {domain_state(dom)}; resume requires it to be paused")
    dom.resume()
    return {"name": name, "state": domain_state(dom)}


def set_autostart(name: str, enabled: bool) -> dict:
    dom = lookup_domain(name)
    dom.setAutostart(1 if enabled else 0)
    return {"name": name, "autostart": bool(dom.autostart())}


# --------------------------------------------------------------------------
# clone
# --------------------------------------------------------------------------


def _clone_disk_paths(root, new_name: str):
    """Compute (old, new) disk path pairs; raises for non-file disks."""

    cfg = get_config()
    copies = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device", "disk") != "disk":
            continue  # keep cdroms/floppies as-is (read-only media)
        if disk.get("type") != "file":
            raise LibvirtMcpError(
                "cannot clone: VM has a non-file-backed disk "
                f"({disk.get('type')}); only file-backed disks are supported"
            )
        source = disk.find("source")
        if source is None or not source.get("file"):
            continue
        target = disk.find("target")
        dev = target.get("dev", "disk") if target is not None else "disk"
        dst = cfg.images_dir / f"{new_name}-{dev}.qcow2"
        if dst.exists():
            raise LibvirtMcpError(f"destination disk already exists: {dst}")
        copies.append((source.get("file"), dst))
    return copies


def clone_vm(name: str, new_name: str) -> dict:
    xmlutil.validate_vm_name(new_name)
    cfg = get_config()
    dom = lookup_domain(name)
    state = dom.state()[0]
    if state not in (_SHUTOFF, _CRASHED):
        raise LibvirtMcpError(
            f"VM '{name}' is {state_name(state)}; stop it before cloning (a consistent "
            "disk copy requires the source VM to be shut off)"
        )

    conn = get_conn()
    try:
        conn.lookupByName(new_name)
        raise LibvirtMcpError(f"a VM named '{new_name}' already exists")
    except libvirt.libvirtError as e:
        if e.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise

    tree = xmlutil.parse(dom.XMLDesc(0))
    root = tree.getroot()

    root.find("name").text = new_name
    for tag in ("uuid", "title"):
        el = root.find(tag)
        if el is not None:
            root.remove(el)
    desc = root.find("description")
    if desc is None:
        desc = xmlutil.ET.SubElement(root, "description")
    desc.text = f"Cloned from '{name}' by libvirt-mcp at {xmlutil.iso_utc(time.time())}"
    for si in root.findall("sysinfo"):
        root.remove(si)
    os_el = root.find("os")
    if os_el is not None:
        smbios = os_el.find("smbios")
        if smbios is not None:
            os_el.remove(smbios)
    for iface in root.findall("./devices/interface"):
        mac = iface.find("mac")
        if mac is not None:
            iface.remove(mac)  # libvirt generates fresh MAC addresses on define

    copies = _clone_disk_paths(root, new_name)

    # UEFI nvram: copy the instance when readable; otherwise (libvirt-managed,
    # qemu-owned) let libvirt create a fresh one from the firmware template
    nvram_el = root.find("./os/nvram")
    nvram = xmlutil.nvram_path(root)
    template = nvram_el.get("template") if nvram_el is not None else None
    nvram_copy = None
    nvram_note = None
    if nvram and nvram_el is not None:
        if os.path.isfile(nvram) and os.access(nvram, os.R_OK):
            nvram_copy = str(cfg.images_dir / f"{new_name}-VARS{Path(nvram).suffix}")
            if os.path.exists(nvram_copy):
                raise LibvirtMcpError(f"destination nvram already exists: {nvram_copy}")
        elif template:
            nvram_el.text = None  # fresh instance from template on define
            nvram_note = (
                f"nvram {nvram} is libvirt-managed and not readable by this user; "
                f"the clone gets a fresh UEFI variable store from {template} "
                "(boot entries are rebuilt on first boot)"
            )
        else:
            raise LibvirtMcpError(
                f"cannot clone the UEFI nvram {nvram}: it is not readable and has no "
                "template attribute to rebuild from"
            )

    cfg.images_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    try:
        for src, dst in copies:
            images.convert_image(src, dst, "qcow2", what=f"cloning disk {src}")
            copied.append(dst)
        if nvram_copy:
            shutil.copy2(nvram, nvram_copy)
            copied.append(Path(nvram_copy))

        for disk in root.findall("./devices/disk"):
            if disk.get("device", "disk") != "disk":
                continue
            source = disk.find("source")
            target = disk.find("target")
            if source is None or target is None:
                continue
            for src, dst in copies:
                if src == source.get("file"):
                    source.set("file", str(dst))
            driver = disk.find("driver")
            if driver is not None:
                driver.set("type", "qcow2")
        if nvram_copy:
            root.find("./os/nvram").text = nvram_copy

        new_dom = conn.defineXML(xmlutil.to_str(tree))
    except BaseException:
        # leave no partial clone behind: drop the copied images
        for path in copied:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    result = {
        "name": new_name,
        "cloned_from": name,
        "disks": [{"from": src, "to": str(dst)} for src, dst in copies],
        "nvram": {"from": nvram, "to": nvram_copy} if nvram_copy else None,
        "state": domain_state(new_dom),
        "note": "cloned VM is defined but not started; use start_vm when ready",
    }
    if nvram_note:
        result["nvram_note"] = nvram_note
    return result


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def _snapshot_disk_files(dom) -> set[str]:
    """Collect disk files referenced by any snapshot (covers external overlays)."""

    files: set[str] = set()
    for snap in dom.listAllSnapshots(0):
        sroot = xmlutil.parse(snap.getXMLDesc()).getroot()
        # <disks> lists external snapshot overlays; <domain> is the config at
        # snapshot time, whose disks point at the files in use back then.
        for disk in list(sroot.findall("./disks/disk")) + list(
            sroot.findall("./domain/devices/disk")
        ):
            if disk.get("device", "disk") != "disk":
                continue
            source = disk.find("source")
            if source is not None and source.get("file"):
                files.add(source.get("file"))
    return files


def delete_vm(name: str) -> dict:
    dom = lookup_domain(name)
    state = dom.state()[0]
    if state not in (_SHUTOFF, _CRASHED):
        raise LibvirtMcpError(
            f"VM '{name}' is {state_name(state)}; stop it before deleting it"
        )

    conn = get_conn()
    tree = xmlutil.parse(dom.XMLDesc(0))
    root = tree.getroot()

    files = set(xmlutil.file_disk_paths(root)) | _snapshot_disk_files(dom)
    nvram = xmlutil.nvram_path(root)

    # Safety net: never delete a file another VM still references.
    other_users: dict[str, list[str]] = {}
    for other in conn.listAllDomains(0):
        if other.name() == name:
            continue
        for path in xmlutil.file_disk_paths(
            xmlutil.parse(other.XMLDesc(0)).getroot()
        ):
            other_users.setdefault(path, []).append(other.name())

    flags = (
        libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
        | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
        | libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
    )
    try:
        dom.undefineFlags(flags)
    except libvirt.libvirtError:
        dom.undefine()  # very old libvirt fallback

    deleted: list[str] = []
    skipped: list[dict] = []
    for path in sorted(files) + ([nvram] if nvram else []):
        if path in other_users:
            skipped.append({"path": path, "reason": f"in use by {other_users[path]}"})
            continue
        try:
            os.unlink(path)
            deleted.append(path)
        except FileNotFoundError:
            pass  # e.g. nvram already removed by libvirt's UNDEFINE_NVRAM
        except OSError as e:
            skipped.append({"path": path, "reason": str(e)})

    return {
        "name": name,
        "undefine_flags": "managed-save | snapshots-metadata | nvram",
        "deleted_files": deleted,
        "skipped": skipped,
    }

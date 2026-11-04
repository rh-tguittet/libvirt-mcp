"""Domain XML building, parsing and editing (pure stdlib, no libvirt needed).

libvirt's domain XML is order-sensitive at the top level, so helpers that
insert elements use the canonical ordering from
https://libvirt.org/formatdomain.html
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

AGENT_CHANNEL_NAME = "org.qemu.guest_agent.0"

#: top-level tags that come *after* <memoryBacking> in the canonical order
_AFTER_MEMORY_BACKING = (
    "vcpu",
    "iothreads",
    "iothreadids",
    "cputune",
    "resource",
    "sysinfo",
    "os",
    "features",
    "cpu",
    "clock",
    "on_poweroff",
    "on_reboot",
    "on_crash",
    "pm",
    "devices",
)

VM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def parse(xml_str: str) -> ET.ElementTree:
    return ET.ElementTree(ET.fromstring(xml_str))


def to_str(tree: ET.ElementTree) -> str:
    return ET.tostring(tree.getroot(), encoding="unicode")


def validate_vm_name(name: str) -> None:
    if not VM_NAME_RE.match(name) or len(name) > 63:
        raise ValueError(
            f"invalid VM name {name!r}: use up to 63 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )


def validate_mount_tag(tag: str) -> None:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", tag) or len(tag) > 64:
        raise ValueError(
            f"invalid mount tag {tag!r}: use up to 64 letters, digits, '.', '_' or '-'"
        )


# --------------------------------------------------------------------------
# reading domain XML
# --------------------------------------------------------------------------


def disk_entries(root: ET.Element) -> list[dict]:
    """Summarize <devices>/<disk> entries."""

    out = []
    for disk in root.findall("./devices/disk"):
        target = disk.find("target")
        driver = disk.find("driver")
        source = disk.find("source")
        entry = {
            "device": disk.get("device", "disk"),  # disk | cdrom | floppy | lun
            "type": disk.get("type"),  # file | block | volume | dir | network
            "bus": target.get("bus") if target is not None else None,
            "dev": target.get("dev") if target is not None else None,
            "driver_type": driver.get("type") if driver is not None else None,
            "path": None,
            "source_attr": None,
            "readonly": disk.find("readonly") is not None,
            "shareable": disk.find("shareable") is not None,
        }
        if source is not None:
            for attr in ("file", "dev", "dir", "pool"):
                if source.get(attr):
                    entry["path"] = source.get(attr)
                    entry["source_attr"] = attr
                    break
        out.append(entry)
    return out


def file_disk_paths(root: ET.Element) -> list[str]:
    """Absolute paths of file-backed hard disks (cdroms/floppies excluded)."""

    return [
        e["path"]
        for e in disk_entries(root)
        if e["device"] == "disk" and e["type"] == "file" and e["path"]
    ]


def nvram_path(root: ET.Element) -> str | None:
    nv = root.find("./os/nvram")
    return nv.text if nv is not None and nv.text else None


def has_agent_channel(root: ET.Element) -> bool:
    for channel in root.findall("./devices/channel"):
        target = channel.find("target")
        if target is not None and target.get("name") == AGENT_CHANNEL_NAME:
            return True
    return False


def os_info(root: ET.Element) -> dict:
    os_el = root.find("./os")
    type_el = os_el.find("type") if os_el is not None else None
    return {
        "arch": type_el.get("arch") if type_el is not None else None,
        "machine": type_el.get("machine") if type_el is not None else None,
        "firmware": os_el.get("firmware", "bios") if os_el is not None else None,
        "boot_devs": [b.get("dev") for b in os_el.findall("boot")] if os_el is not None else [],
    }


def interfaces_xml(root: ET.Element) -> list[dict]:
    out = []
    for iface in root.findall("./devices/interface"):
        source = iface.find("source")
        mac = iface.find("mac")
        model = iface.find("model")
        out.append(
            {
                "type": iface.get("type"),
                "source": (
                    source.get("network") or source.get("bridge") or source.get("dev")
                    if source is not None
                    else None
                ),
                "mac": mac.get("address") if mac is not None else None,
                "model": model.get("type") if model is not None else None,
            }
        )
    return out


def filesystems(root: ET.Element) -> list[dict]:
    """Summarize <filesystem> devices (virtiofs shares and friends)."""

    out = []
    for fs in root.findall("./devices/filesystem"):
        source = fs.find("source")
        target = fs.find("target")
        binary = fs.find("binary")
        driver = fs.find("driver")
        out.append(
            {
                "type": fs.get("type"),
                "accessmode": fs.get("accessmode"),
                "driver_type": driver.get("type") if driver is not None else None,
                "source_dir": source.get("dir") if source is not None else None,
                "source_socket": source.get("socket") if source is not None else None,
                "mount_tag": target.get("dir") if target is not None else None,
                "readonly": fs.find("readonly") is not None,
                "binary_path": binary.get("path") if binary is not None else None,
            }
        )
    return out


def memory_backing_summary(root: ET.Element) -> str | None:
    mb = root.find("memoryBacking")
    if mb is None:
        return None
    parts = []
    huge = mb.find("hugepages")
    if huge is not None:
        parts.append("hugepages")
    source = mb.find("source")
    if source is not None:
        parts.append(f"source={source.get('type')}")
    access = mb.find("access")
    if access is not None:
        parts.append(f"access={access.get('mode')}")
    return ", ".join(parts) if parts else "present"


# --------------------------------------------------------------------------
# editing domain XML (virtiofs support)
# --------------------------------------------------------------------------


def _insert_memory_backing(root: ET.Element) -> ET.Element:
    mb = ET.Element("memoryBacking")
    for tag in _AFTER_MEMORY_BACKING:
        anchor = root.find(tag)
        if anchor is not None:
            root.insert(list(root).index(anchor), mb)
            return mb
    devices = root.find("devices")
    root.insert(list(root).index(devices), mb)
    return mb


def ensure_shared_memory_backing(root: ET.Element) -> str:
    """Make sure guest RAM is shared (required by vhost-user devices, e.g. virtiofs).

    Returns a human-readable description of what was (or was not) changed.
    """

    mb = root.find("memoryBacking")
    if mb is None:
        mb = _insert_memory_backing(root)
    changes = []
    if mb.find("source") is None and mb.find("hugepages") is None:
        ET.SubElement(mb, "source", {"type": "memfd"})
        changes.append("added <source type='memfd'/>")
    access = mb.find("access")
    if access is None:
        ET.SubElement(mb, "access", {"mode": "shared"})
        changes.append("added <access mode='shared'/>")
    elif access.get("mode") != "shared":
        access.set("mode", "shared")
        changes.append("set <access mode='shared'/>")
    return "; ".join(changes) if changes else "already configured"


def strip_shared_memory_backing(root: ET.Element) -> bool:
    """Remove the shared-memory backing if (and only if) we added it ourselves.

    Returns True when the <memoryBacking> element was removed.
    """

    mb = root.find("memoryBacking")
    if mb is None:
        return False
    children = list(mb)
    # Only strip a plain "memfd + shared" backing that has nothing else going on.
    is_ours = all(
        (c.tag == "source" and c.get("type") == "memfd")
        or (c.tag == "access" and c.get("mode") == "shared")
        for c in children
    )
    if is_ours:
        root.remove(mb)
        return True
    return False


def add_virtiofs_device(
    root: ET.Element,
    host_dir: str,
    mount_tag: str,
    accessmode: str = "passthrough",
    readonly: bool = False,
) -> None:
    """Append a virtiofs <filesystem> device to <devices>."""

    for fs in filesystems(root):
        if fs["mount_tag"] == mount_tag:
            raise ValueError(f"mount tag '{mount_tag}' is already used by another share")
        if fs["source_dir"] and fs["source_dir"] == host_dir:
            raise ValueError(f"host directory {host_dir} is already shared with this VM")

    devices = root.find("devices")
    if devices is None:
        raise ValueError("domain XML has no <devices> section")

    # Note: no model='virtiofs' attribute — current libvirt rejects it; the
    # driver type is what selects virtiofs (the default is 9p).
    fs = ET.SubElement(
        devices, "filesystem", {"type": "mount", "accessmode": accessmode}
    )
    ET.SubElement(fs, "driver", {"type": "virtiofs", "queue": "1024"})
    ET.SubElement(fs, "source", {"dir": host_dir})
    ET.SubElement(fs, "target", {"dir": mount_tag})
    if readonly:
        ET.SubElement(fs, "readonly")


def remove_virtiofs_device(root: ET.Element, mount_tag: str) -> bool:
    """Remove the virtiofs share with the given mount tag. Returns True if found."""

    devices = root.find("devices")
    if devices is None:
        return False
    for fs in devices.findall("filesystem"):
        target = fs.find("target")
        if target is not None and target.get("dir") == mount_tag:
            devices.remove(fs)
            return True
    return False


# --------------------------------------------------------------------------
# building domain XML for new VMs
# --------------------------------------------------------------------------


def build_domain_xml(
    *,
    name: str,
    memory_mib: int,
    vcpus: int,
    disk_path: str,
    arch: str = "x86_64",
    network: str | None = "default",
    firmware: str = "bios",
    description: str | None = None,
) -> str:
    """Build a minimal, cloud-image-friendly domain XML."""

    validate_vm_name(name)
    machine = "virt" if arch == "aarch64" else "q35"

    domain = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(domain, "name").text = name
    if description:
        ET.SubElement(domain, "description").text = description
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(memory_mib)
    ET.SubElement(domain, "currentMemory", {"unit": "MiB"}).text = str(memory_mib)
    ET.SubElement(domain, "vcpu", {"placement": "static"}).text = str(vcpus)

    os_el = ET.SubElement(domain, "os")
    if firmware == "efi":
        os_el.set("firmware", "efi")
    ET.SubElement(os_el, "type", {"arch": arch, "machine": machine}).text = "hvm"
    ET.SubElement(os_el, "boot", {"dev": "hd"})

    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")

    ET.SubElement(domain, "cpu", {"mode": "host-passthrough", "check": "none"})
    ET.SubElement(domain, "clock", {"offset": "utc"})
    ET.SubElement(domain, "on_poweroff").text = "destroy"
    ET.SubElement(domain, "on_reboot").text = "restart"
    ET.SubElement(domain, "on_crash").text = "destroy"

    devices = ET.SubElement(domain, "devices")

    disk = ET.SubElement(
        devices, "disk", {"type": "file", "device": "disk"}
    )
    ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2", "discard": "unmap"})
    ET.SubElement(disk, "source", {"file": disk_path})
    ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})

    if network and network.lower() != "none":
        iface = ET.SubElement(
            devices, "interface", {"type": "network"}
        )
        ET.SubElement(iface, "source", {"network": network})
        ET.SubElement(iface, "model", {"type": "virtio"})

    channel = ET.SubElement(devices, "channel", {"type": "unix"})
    ET.SubElement(channel, "source", {"mode": "bind"})
    ET.SubElement(channel, "target", {"type": "virtio", "name": AGENT_CHANNEL_NAME})

    serial = ET.SubElement(devices, "serial", {"type": "pty"})
    ET.SubElement(serial, "target", {"port": "0"})
    console = ET.SubElement(devices, "console", {"type": "pty"})
    ET.SubElement(console, "target", {"type": "serial", "port": "0"})

    ET.SubElement(devices, "memballoon", {"model": "virtio"})
    # systemd guests (>= 256) generate an sshd-vsock.socket listener as soon
    # as they see this device; exec_in_vm's SSH fallback then reaches them
    # via qemu:system/<name> without ever knowing their IP
    ET.SubElement(devices, "vsock", {"model": "virtio"})

    return ET.tostring(domain, encoding="unicode")


def iso_utc(ts: float | int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

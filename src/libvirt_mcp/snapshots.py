"""Snapshot management (create / list / restore / delete) via libvirt."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from libvirt_mcp import compat, xmlutil
from libvirt_mcp.connection import lookup_domain
from libvirt_mcp.util import LibvirtMcpError, domain_state

libvirt = compat.libvirt

_SNAP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_RUNNING = libvirt.VIR_DOMAIN_RUNNING


def _snapshot_xml(name: str, description: str) -> str:
    snap = ET.Element("domainsnapshot")
    ET.SubElement(snap, "name").text = name
    if description:
        ET.SubElement(snap, "description").text = description
    return ET.tostring(snap, encoding="unicode")


def list_snapshots(name: str) -> dict:
    dom = lookup_domain(name)
    snaps = []
    for snap in dom.listAllSnapshots(0):
        sroot = xmlutil.parse(snap.getXMLDesc()).getroot()
        desc = sroot.find("description")
        state_el = sroot.find("state")
        created_el = sroot.find("creationTime")
        snaps.append(
            {
                "name": snap.getName(),
                "description": desc.text if desc is not None else None,
                "current": bool(snap.isCurrent()),
                "created": (
                    xmlutil.iso_utc(int(created_el.text))
                    if created_el is not None and created_el.text
                    else None
                ),
                "state_at_creation": state_el.text if state_el is not None else None,
            }
        )
    snaps.sort(key=lambda s: s["created"] or "")
    return {"name": name, "snapshots": snaps}


def create_snapshot(
    name: str,
    snapshot_name: str | None = None,
    description: str = "",
    disk_only: bool = False,
    quiesce: bool = True,
) -> dict:
    dom = lookup_domain(name)
    if snapshot_name is None:
        snapshot_name = time.strftime("snap-%Y%m%d-%H%M%S")
    if not _SNAP_NAME_RE.match(snapshot_name):
        raise LibvirtMcpError(
            f"invalid snapshot name {snapshot_name!r}: use letters, digits, '.', '_' or '-'"
        )
    try:
        dom.snapshotLookupByName(snapshot_name, 0)
        raise LibvirtMcpError(
            f"snapshot '{snapshot_name}' already exists for VM '{name}'"
        )
    except libvirt.libvirtError as e:
        if e.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT:
            raise

    running = dom.state()[0] == _RUNNING
    flags = 0
    if disk_only:
        flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC

    xml = _snapshot_xml(snapshot_name, description)
    notes = []
    # quiesce (agent-assisted filesystem freeze) is only valid for external
    # disk-only snapshots on current libvirt
    if quiesce and running and disk_only:
        try:
            snap = dom.snapshotCreateXML(xml, flags | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_AGENT_UNRESPONSIVE or "agent" in str(e).lower():
                notes.append("quiesce skipped (guest agent not responding)")
                snap = dom.snapshotCreateXML(xml, flags)
            else:
                raise
    else:
        try:
            snap = dom.snapshotCreateXML(xml, flags)
        except libvirt.libvirtError as e:
            if not disk_only:
                raise LibvirtMcpError(
                    f"{e}. Note: full internal snapshots are unsupported in some "
                    "configurations (e.g. UEFI/pflash guests); try disk_only=true "
                    "for an external disk-only snapshot."
                ) from e
            raise

    return {
        "name": name,
        "snapshot": snap.getName(),
        "current": True,
        "state": domain_state(dom),
        "disk_only": disk_only,
        "notes": notes,
        "description": "disk-only (external) snapshot" if disk_only else "full snapshot (disk + memory, internal)",
    }


def restore_snapshot(name: str, snapshot_name: str, force: bool = False) -> dict:
    dom = lookup_domain(name)
    try:
        snap = dom.snapshotLookupByName(snapshot_name, 0)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT:
            raise LibvirtMcpError(
                f"snapshot '{snapshot_name}' does not exist for VM '{name}'. "
                "Use list_snapshots to see available snapshots."
            ) from None
        raise

    flags = libvirt.VIR_DOMAIN_SNAPSHOT_REVERT_FORCE if force else 0
    dom.revertToSnapshot(snap, flags)
    return {
        "name": name,
        "restored_snapshot": snapshot_name,
        "state": domain_state(dom),
        "warning": "any changes made since the snapshot were discarded",
    }


def delete_snapshot(name: str, snapshot_name: str, children: bool = False) -> dict:
    dom = lookup_domain(name)
    try:
        snap = dom.snapshotLookupByName(snapshot_name, 0)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT:
            raise LibvirtMcpError(
                f"snapshot '{snapshot_name}' does not exist for VM '{name}'"
            ) from None
        raise

    flags = libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_CHILDREN if children else 0
    snap.delete(flags)
    return {
        "name": name,
        "deleted_snapshot": snapshot_name,
        "children_deleted": children,
    }

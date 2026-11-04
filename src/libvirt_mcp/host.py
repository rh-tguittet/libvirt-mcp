"""Hypervisor host information: node, storage pools, networks."""

from __future__ import annotations

from libvirt_mcp import compat, xmlutil
from libvirt_mcp.connection import get_conn
from libvirt_mcp.util import human_bytes, libvirt_version_string

libvirt = compat.libvirt


def host_info() -> dict:
    conn = get_conn()
    info = conn.getInfo()  # [model, memoryKiB, cpus, mhz, nodes, sockets, cores, threads]

    pools = []
    try:
        all_pools = conn.listAllStoragePools(0)
    except libvirt.libvirtError:
        all_pools = []
    for pool in all_pools:
        entry = {"name": pool.name(), "active": bool(pool.isActive())}
        try:
            pi = pool.info()  # [state, capacity, allocation, available]
            entry.update(
                capacity=human_bytes(pi[1]),
                allocation=human_bytes(pi[2]),
                available=human_bytes(pi[3]),
            )
        except libvirt.libvirtError:
            pass
        pools.append(entry)

    networks = []
    try:
        all_nets = conn.listAllNetworks(0)
    except libvirt.libvirtError:
        all_nets = []
    for net in all_nets:
        entry = {"name": net.name(), "active": bool(net.isActive())}
        root = xmlutil.parse(net.XMLDesc(0)).getroot()
        bridge = root.find("bridge")
        if bridge is not None:
            entry["bridge"] = bridge.get("name")
        ips = []
        for ip in root.findall("ip"):
            e = {
                "address": ip.get("address"),
                "netmask": ip.get("netmask"),
                "prefix": ip.get("prefix"),
                "family": ip.get("family", "ipv4"),
            }
            rng = ip.find("dhcp/range")
            if rng is not None:
                e["dhcp_range"] = [rng.get("start"), rng.get("end")]
            ips.append(e)
        entry["ips"] = ips
        networks.append(entry)

    try:
        free_mem = conn.getFreeMemory()
    except (libvirt.libvirtError, AttributeError):
        free_mem = None

    return {
        "hostname": conn.getHostname(),
        "uri": conn.getURI(),
        "hypervisor": conn.getType(),
        "libvirt_version": libvirt_version_string(conn.getLibVersion()),
        "cpu_model": info[0],
        "cpus": info[2],
        "cpu_mhz": info[3],
        "cpu_topology": {
            "nodes": info[4],
            "sockets": info[5],
            "cores_per_socket": info[6],
            "threads_per_core": info[7],
        },
        # the python bindings report node memory in MiB here (virNodeGetInfo
        # itself returns KiB — virsh nodeinfo agrees; conn.getInfo() does not),
        # while domain info (dom.info()[1]) stays KiB
        "memory_mb": info[1],
        "free_memory_mb": free_mem // 1024**2 if free_mem else None,  # bytes -> MiB
        "storage_pools": pools,
        "networks": networks,
    }

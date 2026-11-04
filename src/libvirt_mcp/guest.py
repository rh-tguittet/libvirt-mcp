"""Run commands inside a VM: qemu guest agent first, SSH as fallback.

The agent path uses the libvirt QEMU extension bindings
(``libvirt_qemu.qemuAgentCommand``) to drive the ``guest-exec`` /
``guest-exec-status`` QAPI commands.
"""

from __future__ import annotations

import base64
import binascii
import json
import subprocess
import time

from libvirt_mcp import compat
from libvirt_mcp.config import get_config
from libvirt_mcp.connection import lookup_domain
from libvirt_mcp.util import LibvirtMcpError, truncate

libvirt = compat.libvirt

_RUNNING = libvirt.VIR_DOMAIN_RUNNING


# --------------------------------------------------------------------------
# guest agent (pure helpers first, so they are easy to unit test)
# --------------------------------------------------------------------------


def decode_exec_status(status: dict) -> dict:
    """Turn a guest-exec-status result into {exit_code, stdout, stderr, signal}."""

    def b64(field: str) -> str:
        data = status.get(field) or ""
        if not data:
            return ""
        try:
            return base64.b64decode(data).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ""

    return {
        "exit_code": status.get("exitcode"),
        "stdout": b64("out-data"),
        "stderr": b64("err-data"),
        "signal": status.get("signal"),
    }


def agent_rpc(dom, payload: dict, timeout: int = 30) -> dict:
    """Perform one QEMU guest agent RPC through libvirt."""

    if compat.libvirt_qemu is None:
        raise LibvirtMcpError(
            "the libvirt_qemu python bindings are not available; "
            "guest-agent execution is disabled (install python3-libvirt fully)"
        )
    raw = compat.libvirt_qemu.qemuAgentCommand(dom, json.dumps(payload), timeout, 0)
    resp = json.loads(raw)
    if "error" in resp:
        desc = resp["error"].get("desc", "unknown guest agent error")
        raise LibvirtMcpError(f"guest agent error: {desc}")
    return resp


def agent_ping(dom, timeout: int = 10) -> bool:
    try:
        agent_rpc(dom, {"execute": "guest-ping"}, timeout)
        return True
    except (LibvirtMcpError, libvirt.libvirtError):
        return False


def agent_exec(dom, command: str, timeout: int) -> dict:
    """Execute a command via the guest agent and wait for it to finish."""

    resp = agent_rpc(
        dom,
        {
            "execute": "guest-exec",
            "arguments": {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            },
        },
        timeout=30,
    )
    pid = resp["return"]["pid"]

    deadline = time.monotonic() + timeout
    while True:
        status = agent_rpc(
            dom, {"execute": "guest-exec-status", "arguments": {"pid": pid}}, timeout=30
        )["return"]
        if status.get("exited"):
            result = decode_exec_status(status)
            result["timed_out"] = False
            return result
        if time.monotonic() > deadline:
            raise LibvirtMcpError(
                f"timed out after {timeout}s waiting for the command to finish inside "
                "the guest (the process may still be running there)"
            )
        time.sleep(0.5)


# --------------------------------------------------------------------------
# network interfaces
# --------------------------------------------------------------------------


def interface_addresses(dom) -> list[dict]:
    """Interface/address report: guest agent when available, else DHCP/ARP."""

    sources = (
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP,
    )
    for source in sources:
        try:
            ifaces = dom.interfaceAddresses(source)
        except libvirt.libvirtError:
            continue
        out = []
        for name, data in (ifaces or {}).items():
            if name == "lo":
                continue
            ipv4, ipv6 = [], []
            for addr in data.get("addrs", []):
                if addr["addr"].startswith(("127.", "::1")):
                    continue
                if addr.get("type") == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                    ipv4.append(addr["addr"])
                else:
                    ipv6.append(addr["addr"])
            out.append(
                {
                    "interface": name,
                    "hwaddr": data.get("hardware"),
                    "ipv4": sorted(set(ipv4)),
                    "ipv6": sorted(set(ipv6)),
                }
            )
        if out:
            return out
    return []


# --------------------------------------------------------------------------
# SSH fallback
# --------------------------------------------------------------------------


def ssh_exec(host: str, command: str, user: str, timeout: int | None = None) -> dict:
    cfg = get_config()
    key_path = cfg.ssh_key
    if not key_path.exists():
        raise LibvirtMcpError(
            f"SSH key {key_path} not found. The libvirt-mcp SSH key is required "
            f"for the SSH fallback; generate it with:\n"
            f"  ssh-keygen -t ed25519 -N '' -f {key_path}"
        )
    args = [
        "ssh",
        "-i",
        str(key_path),
        "-p",
        str(cfg.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "LogLevel=ERROR",
        f"{user}@{host}",
        command,
    ]
    effective_timeout = (timeout or cfg.exec_timeout) + 20
    try:
        cp = subprocess.run(
            args, capture_output=True, text=True, timeout=effective_timeout
        )
    except subprocess.TimeoutExpired as e:
        raise LibvirtMcpError(
            f"ssh to {host} timed out after {effective_timeout}s"
        ) from e
    return {"exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}


# --------------------------------------------------------------------------
# combined entry point
# --------------------------------------------------------------------------


def exec_in_vm(
    name: str,
    command: str,
    ssh_user: str,
    timeout: int | None = None,
    use_ssh_fallback: bool = True,
    force_ssh: bool = False,
) -> dict:
    dom = lookup_domain(name)
    if dom.state()[0] != _RUNNING:
        raise LibvirtMcpError(
            f"VM '{name}' is not running; commands can only run in running VMs"
        )
    cfg = get_config()
    timeout = timeout or cfg.exec_timeout

    # Agent path: commands always run as root in the guest — the QEMU guest
    # agent protocol has no user concept. force_ssh skips it, e.g. to run as a
    # non-root ssh_user, or on SELinux-hardened guests (RHEL 10+) where agent
    # commands run in a confined context.
    if not force_ssh and agent_ping(dom):
        result = agent_exec(dom, command, timeout)
        result["method"] = "qemu-guest-agent"
        result["command"] = command
        result["stdout"] = truncate(result["stdout"])
        result["stderr"] = truncate(result["stderr"])
        return result

    if not force_ssh and not use_ssh_fallback:
        raise LibvirtMcpError(
            f"guest agent is not available in VM '{name}' and SSH fallback was disabled"
        )

    errors = []

    def _try_ssh(host: str) -> dict | None:
        """One SSH attempt; returns None and records the failure otherwise."""
        try:
            result = ssh_exec(host, command, user=ssh_user, timeout=timeout)
        except LibvirtMcpError as e:
            errors.append(f"{host}: {e}")
            return None
        if result["exit_code"] == 255:
            # ssh itself failed (auth refused, user unknown, unreachable) —
            # the command never ran in the guest
            errors.append(f"{host}: {result['stderr'].strip() or 'ssh connection failed'}")
            return None
        result["method"] = "ssh"
        result["command"] = command
        result["stdout"] = truncate(result["stdout"])
        result["stderr"] = truncate(result["stderr"])
        # the qemu:system proxy address is not an IP — keep the fields
        # separate so both can be reported when the vsock transport wins
        if host.startswith("qemu:"):
            result["vsock"] = host
        else:
            result["ip"] = host
        return result

    ips = sorted({a for i in interface_addresses(dom) for a in i["ipv4"]})

    # The libvirt SSH proxy address (qemu:system/<vm>, VSOCK-backed) works
    # without knowing the VM's IP — try it before the discovered addresses.
    result = _try_ssh(f"qemu:system/{name}")
    if result:
        # vsock won; report the guest's IP alongside for callers that want
        # a routable address
        if ips:
            result["ip"] = ips[0]
        return result

    for ip in ips:
        result = _try_ssh(ip)
        if result:
            return result

    raise LibvirtMcpError(
        f"SSH to VM '{name}' as user '{ssh_user}' failed on every address:\n  "
        + "\n  ".join(errors)
    )

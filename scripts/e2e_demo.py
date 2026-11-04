#!/usr/bin/env python3
"""End-to-end exercise of libvirt-mcp against the real hypervisor.

Creates a throwaway VM from a local cloud image, then: runs commands in it via
the guest agent, tests the SSH fallback, snapshots + restores it, shares a host
directory via virtiofs, clones it, and finally deletes everything.

Safe to re-run: it cleans up leftovers from previous runs and always removes
the VMs it created (set LIBVIRT_MCP_E2E_KEEP=1 to keep them for inspection).

Usage:
    uv run python scripts/e2e_demo.py [base-image-filename]
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from mcp import Client

from libvirt_mcp.server import mcp

VM = "mcp-e2e"
CLONE = "mcp-e2e-clone"
SNAP = "e2e-snap"
SHARE = Path("/tmp/opencode/libvirt-mcp-e2e-share")
# Must be a PRISTINE cloud image. A disk that belongs to a VM (see the
# used_by pre-flight below) carries that guest's state — e.g. an fstab entry
# for a virtiofs share — which parks fresh boots in an emergency shell.
DEFAULT_BASE = "Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2"
# RHEL 10 / Rocky 10 images are UEFI-only, hence firmware=efi
FIRMWARE = "efi"


async def call(client, tool, args=None, must=True):
    result = await client.call_tool(tool, args or {})
    if result.is_error:
        if must:
            raise RuntimeError(f"{tool} failed:\n{result.content[0].text}")
        return None
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


async def wait_agent(client, timeout=240):
    """Poll exec_in_vm (agent only) until the guest agent answers."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await call(
            client,
            "exec_in_vm",
            {"name": VM, "command": "echo agent-ok", "ssh_user": "root", "timeout": 10, "use_ssh_fallback": False},
            must=False,
        )
        if r and "agent-ok" in r["stdout"]:
            return r
        await asyncio.sleep(5)
    raise RuntimeError("guest agent never came up")


async def cleanup(client, *names):
    for name in names:
        vms = await call(client, "list_vms")
        if any(v["name"] == name for v in vms):
            await call(client, "stop_vm", {"name": name, "force": True, "confirm": True})
            await call(client, "delete_vm", {"name": name, "confirm": True})
            print(f"      cleaned up leftover VM '{name}'")


async def main():
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    t_start = time.time()

    async with Client(mcp) as client:
        print(f"[0/8] cleaning up leftovers")
        await cleanup(client, VM, CLONE)
        if SHARE.exists():
            for f in SHARE.iterdir():
                f.unlink()

        imgs = await call(client, "list_images")
        used_by = next((i["used_by"] for i in imgs if i["name"] == base), [])
        if used_by:
            raise RuntimeError(
                f"base image '{base}' is the live disk of {', '.join(used_by)}; "
                "clone a pristine cloud image instead — a used disk carries guest "
                "state (fstab mounts, host-specific config) that hangs fresh boots"
            )

        print(f"[1/8] create_vm from {base} (copy + virt-customize — takes a few minutes)")
        t0 = time.time()
        info = await call(
            client,
            "create_vm",
            {
                "name": VM,
                "base_image": base,
                "vcpus": 2,
                "memory_mb": 1024,
                "firmware": FIRMWARE,
            },
        )
        print(f"      done in {time.time() - t0:.0f}s: {info['state']}, disk={info['disk_path']}")

        print("[2/8] boot + guest agent")
        r = await wait_agent(client)
        print(f"      exec_in_vm via {r['method']}: exit={r['exit_code']} stdout={r['stdout']!r}")

        print("[3/8] snapshot while running, then restore")
        snap = await call(
            client, "create_snapshot", {"name": VM, "snapshot_name": SNAP}, must=False
        )
        disk_only = snap is None
        if disk_only:
            print("      full snapshot unsupported here (pflash/UEFI); retrying disk_only=true")
            snap = await call(
                client,
                "create_snapshot",
                {"name": VM, "snapshot_name": SNAP, "disk_only": True},
            )
        snaps = await call(client, "list_snapshots", {"name": VM})
        assert any(s["name"] == SNAP for s in snaps["snapshots"]), "snapshot not listed"
        await call(client, "exec_in_vm", {"name": VM, "command": "touch /root/after-snapshot", "ssh_user": "root"})
        await call(client, "restore_snapshot", {"name": VM, "snapshot_name": SNAP, "confirm": True})
        if disk_only:
            await call(client, "start_vm", {"name": VM})
            await wait_agent(client)
        else:
            await asyncio.sleep(5)
        r = await call(
            client,
            "exec_in_vm",
            {
                "name": VM,
                "ssh_user": "root",
                "command": "test -e /root/after-snapshot && echo present || echo gone",
            },
        )
        assert "gone" in r["stdout"], "restore did not rewind the guest disk"
        print(f"      snapshot mode={'disk_only' if disk_only else 'full(memory)'}, restore rewound the disk ✓")

        print("[4/8] SSH fallback (agent stopped inside the guest)")
        # try to stop the agent from inside; the agent's own context is
        # confined, so this may fail — diagnose, then fall back to raw SSH
        r = await call(
            client,
            "exec_in_vm",
            {
                "name": VM,
                "ssh_user": "root",
                "command": (
                    "systemctl is-active qemu-guest-agent.service; "
                    "systemctl is-enabled qemu-guest-agent.service; "
                    "ps -o pid,ppid,cmd -C qemu-ga; "
                    "systemctl --no-block stop qemu-guest-agent.service 2>&1; echo stop-rc=$?"
                ),
                "timeout": 30,
            },
        )
        print(f"      agent diagnostics: {r['stdout'].strip()}")
        if r["stderr"]:
            print(f"      diagnostics stderr: {r['stderr'].strip()}")
        await asyncio.sleep(5)
        r = await call(client, "exec_in_vm", {"name": VM, "command": "echo ssh-ok", "ssh_user": "root", "timeout": 30})
        if r["method"] == "qemu-guest-agent":
            print("      agent still up; stopping it over raw SSH instead")
            from libvirt_mcp.guest import ssh_exec

            info = await call(client, "get_vm_info", {"name": VM})
            ip = info["ipv4"][0]
            res = await asyncio.to_thread(ssh_exec, ip, "systemctl stop qemu-guest-agent", "root", 30)
            print(f"      ssh stop: exit={res['exit_code']} stderr={res['stderr'].strip()[:200]}")
            await asyncio.sleep(3)
            r = await call(client, "exec_in_vm", {"name": VM, "command": "echo ssh-ok", "ssh_user": "root", "timeout": 30})
        assert r["method"] == "ssh" and "ssh-ok" in r["stdout"], f"ssh fallback failed: {r}"
        print(f"      exec_in_vm via {r['method']} to {r.get('vsock') or r.get('ip')}: stdout={r['stdout']!r} ✓")
        # bring the agent back for the remaining steps
        from libvirt_mcp.guest import ssh_exec as _ssh

        info = await call(client, "get_vm_info", {"name": VM})
        await asyncio.to_thread(_ssh, info["ipv4"][0], "systemctl start qemu-guest-agent", "root", 30)
        await asyncio.sleep(3)

        print("[5/8] virtiofs file share")
        await call(client, "stop_vm", {"name": VM, "wait": True})
        share = await call(client, "add_fileshare", {"name": VM, "host_path": str(SHARE)})
        tag = share["mount_tag"]
        print(f"      share added: {SHARE} <-> tag '{tag}' (memory backing: {share['memory_backing']})")
        await call(client, "start_vm", {"name": VM})
        await wait_agent(client)
        r = await call(
            client,
            "exec_in_vm",
            {
                "name": VM,
                "ssh_user": "root",
                "command": (
                    f"mkdir -p /mnt/{tag} && mount -t virtiofs {tag} /mnt/{tag} && "
                    f"echo from-guest > /mnt/{tag}/hello.txt && cat /mnt/{tag}/hello.txt"
                ),
                "timeout": 60,
                "force_ssh": True,
            },
        )
        print(f"      mount+write result (via {r['method']}): exit={r['exit_code']} stdout={r['stdout']!r} stderr={r['stderr']!r}")
        if r["exit_code"] != 0:
            d = await call(
                client,
                "exec_in_vm",
                {
                    "name": VM,
                    "ssh_user": "root",
                    "command": (
                        f"dmesg | grep -iE 'virtio.?fs|vhost' | tail -10; "
                        f"ls -la /mnt/{tag} 2>&1; mount | grep virtiofs; "
                        "lsmod | grep virtio"
                    ),
                    "timeout": 30,
                },
            )
            print(f"      guest diagnostics:\n{d['stdout']}")
        assert r["exit_code"] == 0, "virtiofs mount failed in guest"
        assert (SHARE / "hello.txt").read_text().strip() == "from-guest", "guest->host share failed"
        (SHARE / "from-host.txt").write_text("host-data")
        r = await call(
            client,
            "exec_in_vm",
            {"name": VM, "command": f"cat /mnt/{tag}/from-host.txt", "ssh_user": "root", "force_ssh": True},
        )
        print(f"      read-back result: exit={r['exit_code']} stdout={r['stdout']!r}")
        assert "host-data" in r["stdout"], "host->guest share failed"
        shares = await call(client, "list_fileshares", {"name": VM})
        assert any(s["mount_tag"] == tag for s in shares["shares"])
        print(f"      guest->host and host->guest file exchange over virtiofs ✓")

        print("[6/8] clone")
        await call(client, "stop_vm", {"name": VM, "wait": True})
        clone = await call(client, "clone_vm", {"name": VM, "new_name": CLONE})
        print(f"      cloned to {CLONE}: {clone['disks']}")
        if clone.get("nvram_note"):
            print(f"      nvram: {clone['nvram_note']}")
        await call(client, "start_vm", {"name": CLONE})
        deadline = time.time() + 240
        while time.time() < deadline:
            r = await call(
                client,
                "exec_in_vm",
                {"name": CLONE, "command": "echo clone-ok", "ssh_user": "root", "timeout": 10, "use_ssh_fallback": False},
                must=False,
            )
            if r and "clone-ok" in r["stdout"]:
                break
            await asyncio.sleep(5)
        else:
            raise RuntimeError("clone never booted")
        print(f"      clone booted and answers exec_in_vm ✓")

        print("[7/8] delete (confirm-gated)")
        await call(client, "stop_vm", {"name": CLONE, "wait": True})
        await call(client, "delete_vm", {"name": CLONE, "confirm": True})
        await call(client, "stop_vm", {"name": VM, "wait": True})
        deleted = await call(client, "delete_vm", {"name": VM, "confirm": True})
        print(f"      deleted files: {deleted['deleted_files']}")
        for path in deleted["deleted_files"]:
            assert not Path(path).exists(), f"{path} still exists after delete_vm"

        print("[8/8] final state")
        vms = await call(client, "list_vms")
        names = [v["name"] for v in vms]
        assert VM not in names and CLONE not in names
        print(f"      remaining VMs: {', '.join(names)}")
        print(f"\nALL STEPS PASSED in {time.time() - t_start:.0f}s")


async def _cleanup_only():
    async with Client(mcp) as client:
        await cleanup(client, VM, CLONE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException as e:
        # the in-memory client wraps errors in a TaskGroup; unwrap for clarity
        while isinstance(e, BaseExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        print(f"\nE2E FAILED: {e}", file=sys.stderr)
        keep = os.environ.get("LIBVIRT_MCP_E2E_KEEP") == "1"
        print(
            f"VMs '{VM}'/'{CLONE}' were {'kept' if keep else 'cleaned up'} "
            "(re-run also cleans up leftovers).",
            file=sys.stderr,
        )
        if not keep:
            # best-effort cleanup even on failure
            try:
                asyncio.run(_cleanup_only())
            except Exception:
                pass
        sys.exit(1)

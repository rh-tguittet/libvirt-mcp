"""The MCP server: tool definitions exposed over stdio.

All libvirt interaction happens through the python bindings; long-running
external tools (virt-customize, qemu-img, curl) run in worker threads so the
event loop stays responsive.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from libvirt_mcp import __version__, compat, domains, guest, host, images, snapshots, virtiofs
from libvirt_mcp import customize
from libvirt_mcp.config import get_config
from libvirt_mcp.util import LibvirtMcpError

libvirt = compat.libvirt

_cfg = get_config()

INSTRUCTIONS = f"""\
Manage libvirt/KVM virtual machines ({_cfg.uri}).
Disk images live in {_cfg.images_dir} (see list_images and list_recommended_images).
Create VMs with create_vm (copies a base cloud image, runs virt-customize on the copy \
to set the root password / inject the {_cfg.ssh_key}.pub key / install packages, then \
defines and starts the domain).
Run commands inside VMs with exec_in_vm (qemu guest agent first, SSH with {_cfg.ssh_key} \
as ssh_user as fallback — ssh_user is required, there is no default user).
Destructive operations (delete_vm, forced stop/reboot, restore_snapshot, \
delete_snapshot) require confirm=true.
VMs must be shut off for clone_vm, customize_image and add/remove_fileshare; \
snapshots and start/stop work while running.
""".strip()

mcp = MCPServer(
    "libvirt-mcp",
    version=__version__,
    instructions=INSTRUCTIONS,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True)
MUTATING = ToolAnnotations(destructive_hint=False)


async def _run(fn, /, *args, **kwargs):
    """Run a blocking operation in a thread, translating errors for the model."""

    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ToolError:
        raise
    except LibvirtMcpError as e:
        raise ToolError(str(e)) from None
    except libvirt.libvirtError as e:
        raise ToolError(str(e).strip() or "libvirt error") from None
    except ValueError as e:
        raise ToolError(str(e)) from None


def _require_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise ToolError(
            f"{what} is destructive and needs explicit confirmation. "
            "Call this tool again with confirm=true."
        )


# ==========================================================================
# host & inventory
# ==========================================================================


@mcp.tool(annotations=READ_ONLY)
async def get_host_info() -> dict[str, object]:
    """Information about the hypervisor host: CPUs, memory, libvirt version,
    storage pools and virtual networks (useful for picking a network for new VMs)."""
    return await _run(host.host_info)


@mcp.tool(annotations=READ_ONLY)
async def list_vms() -> list[dict[str, object]]:
    """List all VMs (domains) with state, memory, vCPUs, autostart and IPv4
    addresses (best effort, from DHCP leases/ARP). Use get_vm_info for details."""
    return await _run(domains.list_vms)


@mcp.tool(annotations=READ_ONLY)
async def get_vm_info(
    name: Annotated[str, Field(description="VM name")],
    include_xml: Annotated[bool, Field(description="Include the full libvirt domain XML")] = False,
) -> dict[str, object]:
    """Detailed information about one VM: state, resources, disks, network
    interfaces and IPs, guest agent status, snapshots, file shares."""
    return await _run(domains.get_vm_info, name, include_xml=include_xml)


@mcp.tool(annotations=READ_ONLY)
async def list_images() -> list[dict]:
    """List disk images in the images directory (~/VirtualMachines) with format,
    virtual size, size on disk and which VMs use them."""
    return await _run(images.list_images)


@mcp.tool(annotations=READ_ONLY)
async def list_recommended_images() -> list[dict]:
    """Curated list of recommended cloud images (Ubuntu, Debian, Fedora, Rocky,
    AlmaLinux) that can be downloaded with download_image."""
    return await _run(images.list_recommended_images)


@mcp.tool(annotations=MUTATING)
async def download_image(
    image_id: Annotated[
        str | None, Field(description="Image id from list_recommended_images, e.g. 'debian-13'")
    ] = None,
    url: Annotated[str | None, Field(description="Direct URL of a qcow2/raw cloud image")] = None,
    filename: Annotated[str | None, Field(description="Destination filename (default: from URL)")] = None,
) -> dict[str, object]:
    """Download a cloud image into the images directory for use with create_vm.
    Pass either image_id (recommended) or a direct url."""
    return await _run(images.download_image, image_id=image_id, url=url, filename=filename)


# ==========================================================================
# lifecycle
# ==========================================================================


@mcp.tool(annotations=MUTATING)
async def start_vm(name: Annotated[str, Field(description="VM name")]) -> dict[str, object]:
    """Start a shut-off VM (or resume it if paused)."""
    return await _run(domains.start_vm, name)


@mcp.tool(annotations=DESTRUCTIVE)
async def stop_vm(
    name: Annotated[str, Field(description="VM name")],
    force: Annotated[
        bool, Field(description="Cut power immediately instead of a graceful ACPI shutdown")
    ] = False,
    confirm: Annotated[bool, Field(description="Required (true) when force=true")] = False,
    wait: Annotated[bool, Field(description="Wait until the VM is fully powered off")] = False,
    wait_timeout: Annotated[int, Field(description="Seconds to wait when wait=true", ge=1, le=3600)] = 120,
) -> dict[str, object]:
    """Stop a VM. Graceful ACPI shutdown by default; set force=true (with
    confirm=true) to cut power like pulling the plug. Use wait=true before
    operations that need the VM fully off (clone, customize)."""
    if force:
        _require_confirm(confirm, f"force-stopping VM '{name}'")
    return await _run(domains.stop_vm, name, force=force, wait=wait, wait_timeout=wait_timeout)


@mcp.tool(annotations=DESTRUCTIVE)
async def reboot_vm(
    name: Annotated[str, Field(description="VM name")],
    force: Annotated[bool, Field(description="Hard reset instead of graceful ACPI reboot")] = False,
    confirm: Annotated[bool, Field(description="Required (true) when force=true")] = False,
) -> dict[str, object]:
    """Reboot a running VM (graceful ACPI by default; force=true + confirm=true
    for a hard reset)."""
    if force:
        _require_confirm(confirm, f"force-rebooting VM '{name}'")
    return await _run(domains.reboot_vm, name, force=force)


@mcp.tool(annotations=MUTATING)
async def pause_vm(name: Annotated[str, Field(description="VM name")]) -> dict[str, object]:
    """Pause a running VM (freeze it in RAM; resume with resume_vm)."""
    return await _run(domains.pause_vm, name)


@mcp.tool(annotations=MUTATING)
async def resume_vm(name: Annotated[str, Field(description="VM name")]) -> dict[str, object]:
    """Resume a paused VM."""
    return await _run(domains.resume_vm, name)


@mcp.tool(annotations=MUTATING)
async def set_vm_autostart(
    name: Annotated[str, Field(description="VM name")],
    enabled: Annotated[bool, Field(description="Start the VM automatically when libvirt starts")] = True,
) -> dict[str, object]:
    """Enable or disable autostart for a VM."""
    return await _run(domains.set_autostart, name, enabled)


# ==========================================================================
# snapshots
# ==========================================================================


@mcp.tool(annotations=READ_ONLY)
async def list_snapshots(name: Annotated[str, Field(description="VM name")]) -> dict[str, object]:
    """List snapshots of a VM with creation time and which one is current."""
    return await _run(snapshots.list_snapshots, name)


@mcp.tool(annotations=MUTATING)
async def create_snapshot(
    name: Annotated[str, Field(description="VM name")],
    snapshot_name: Annotated[
        str | None, Field(description="Snapshot name (default: snap-YYYYmmdd-HHMMSS)")
    ] = None,
    description: Annotated[str, Field(description="Free-form description")] = "",
    disk_only: Annotated[
        bool,
        Field(
            description="External disk-only snapshot (works while running, no memory state). "
            "Default: full snapshot including memory (internal)"
        ),
    ] = False,
    quiesce: Annotated[
        bool,
        Field(
            description="Ask the guest agent to freeze filesystems for consistency "
            "(only applies to disk_only snapshots)"
        ),
    ] = True,
) -> dict[str, object]:
    """Create a snapshot of a VM. Can run while the VM is running. Full
    snapshots (default) include memory and restore the running state;
    disk_only snapshots are external overlays without memory."""
    return await _run(
        snapshots.create_snapshot,
        name,
        snapshot_name=snapshot_name,
        description=description,
        disk_only=disk_only,
        quiesce=quiesce,
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def restore_snapshot(
    name: Annotated[str, Field(description="VM name")],
    snapshot_name: Annotated[str, Field(description="Snapshot to restore (see list_snapshots)")],
    confirm: Annotated[bool, Field(description="Must be true; restoring discards current state")] = False,
    force: Annotated[
        bool, Field(description="Allow reverting to a snapshot on a different branch")
    ] = False,
) -> dict[str, object]:
    """Restore a VM to a snapshot. WARNING: everything done since the snapshot
    was taken is discarded. Requires confirm=true."""
    _require_confirm(confirm, f"restoring VM '{name}' to snapshot '{snapshot_name}'")
    return await _run(snapshots.restore_snapshot, name, snapshot_name, force=force)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_snapshot(
    name: Annotated[str, Field(description="VM name")],
    snapshot_name: Annotated[str, Field(description="Snapshot to delete")],
    confirm: Annotated[bool, Field(description="Must be true; deletion is irreversible")] = False,
    children: Annotated[bool, Field(description="Also delete descendant snapshots")] = False,
) -> dict[str, object]:
    """Delete a snapshot (irreversible). Requires confirm=true."""
    _require_confirm(confirm, f"deleting snapshot '{snapshot_name}' of VM '{name}'")
    return await _run(snapshots.delete_snapshot, name, snapshot_name, children=children)


# ==========================================================================
# create / clone / delete
# ==========================================================================


@mcp.tool(annotations=MUTATING)
async def clone_vm(
    name: Annotated[str, Field(description="Existing VM to clone (must be shut off)")],
    new_name: Annotated[str, Field(description="Name for the clone")],
) -> dict[str, object]:
    """Clone a shut-off VM: copies all its disks to new images and defines an
    independent VM with the same configuration (fresh UUID/MACs)."""
    return await _run(domains.clone_vm, name, new_name)


@mcp.tool(annotations=MUTATING)
async def create_vm(
    name: Annotated[str, Field(description="Name for the new VM")],
    base_image: Annotated[
        str,
        Field(
            description="Base disk image: a path, or a filename inside the images dir "
            "(see list_images / download_image)"
        ),
    ],
    vcpus: Annotated[int, Field(description="Virtual CPUs", ge=1, le=64)] = 2,
    memory_mb: Annotated[int, Field(description="Memory in MiB", ge=256, le=262144)] = 2048,
    disk_gb: Annotated[
        float | None,
        Field(description="Grow the disk to this size in GiB (default: keep base image size)", ge=1),
    ] = None,
    root_password: Annotated[
        str | None,
        Field(
            description="Root password to set in the image (optional; SSH key access is "
            "configured anyway). Avoid if you don't need console logins"
        ),
    ] = None,
    packages: Annotated[list[str] | None, Field(description="Packages to install in the image")] = None,
    update: Annotated[bool, Field(description="Update existing packages in the image")] = False,
    run_commands: Annotated[
        list[str] | None, Field(description="Shell commands run in the image during customization")
    ] = None,
    network: Annotated[str, Field(description="libvirt network to attach (or 'none')")] = "default",
    firmware: Annotated[Literal["bios", "efi"], Field(description="Boot firmware")] = "bios",
    start: Annotated[bool, Field(description="Start the VM after creation")] = True,
) -> dict[str, object]:
    """Create a new VM from a base cloud image: copies the image, runs
    virt-customize on the copy (root password, ~/.ssh/vms SSH key for root,
    packages — qemu-guest-agent is always installed), defines the domain and
    starts it. Fails if the ~/.ssh/vms keypair does not exist. The copy can
    take a while for large images; package installation needs network access."""
    return await _run(
        customize.create_vm,
        name,
        base_image,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        root_password=root_password,
        packages=packages,
        update=update,
        run_commands=run_commands,
        network=network,
        firmware=firmware,
        start=start,
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_vm(
    name: Annotated[str, Field(description="VM to delete (must be shut off)")],
    confirm: Annotated[bool, Field(description="Must be true; deletes the VM and its disk files")] = False,
) -> dict[str, object]:
    """Delete a VM permanently: undefines the domain AND deletes its disk image
    files (and snapshot files) that are not used by other VMs. Irreversible —
    requires confirm=true and the VM to be shut off."""
    _require_confirm(confirm, f"deleting VM '{name}'")
    return await _run(domains.delete_vm, name)


# ==========================================================================
# image customization
# ==========================================================================


@mcp.tool(annotations=MUTATING)
async def customize_image(
    image: Annotated[str, Field(description="Image path or filename in the images dir")],
    root_password: Annotated[str | None, Field(description="Root password to set")] = None,
    packages: Annotated[list[str] | None, Field(description="Packages to install")] = None,
    update: Annotated[bool, Field(description="Update existing packages")] = False,
    run_commands: Annotated[list[str] | None, Field(description="Shell commands to run in the image")] = None,
) -> dict[str, object]:
    """Customize an existing disk image with virt-customize (set root password,
    inject the ~/.ssh/vms SSH key, install packages, run commands). The image
    is modified in place; any VM using it must be shut off. Prefer create_vm
    for new VMs. Fails if the ~/.ssh/vms keypair does not exist."""
    return await _run(
        customize.customize_image,
        image,
        root_password=root_password,
        packages=packages,
        update=update,
        run_commands=run_commands,
    )


# ==========================================================================
# command execution inside VMs
# ==========================================================================


@mcp.tool(annotations=MUTATING)
async def exec_in_vm(
    name: Annotated[str, Field(description="VM name (must be running)")],
    command: Annotated[str, Field(description="Shell command to run (via /bin/sh -c in the guest)")],
    ssh_user: Annotated[
        str,
        Field(
            description="Username for the SSH path — required, there is no default. "
            "Used when the guest agent is unavailable or force_ssh=true; an error "
            "is raised if this user cannot connect"
        ),
    ],
    timeout: Annotated[int, Field(description="Seconds to wait for completion", ge=1, le=3600)] = 120,
    use_ssh_fallback: Annotated[
        bool, Field(description="Fall back to SSH (~/.ssh/vms key, as ssh_user) if the guest agent fails")
    ] = True,
    force_ssh: Annotated[
        bool,
        Field(
            description="Skip the guest agent and run via SSH as ssh_user (unconfined). Needed for "
            "privileged operations (mount, systemctl, ...) on SELinux-hardened guests "
            "like RHEL/Rocky 10, where agent commands run in a confined context"
        ),
    ] = False,
) -> dict[str, object]:
    """Run a shell command inside a running VM and return exit code, stdout and
    stderr. Uses the qemu guest agent when available, else SSH as ssh_user
    (required; errors if that user cannot connect): the libvirt SSH proxy
    address (qemu:system/<vm>, VSOCK) is tried first, then the VM's IP
    addresses. SSH results carry the winning transport's address — "vsock"
    for the proxy address, "ip" for the guest IP, and both when vsock won.
    Note: agent commands always run as root in the guest — the agent
    protocol has no user concept; use force_ssh=true with a non-root
    ssh_user to avoid that."""
    return await _run(
        guest.exec_in_vm,
        name,
        command,
        ssh_user,
        timeout=timeout,
        use_ssh_fallback=use_ssh_fallback,
        force_ssh=force_ssh,
    )


# ==========================================================================
# virtiofs file shares
# ==========================================================================


@mcp.tool(annotations=READ_ONLY)
async def list_fileshares(name: Annotated[str, Field(description="VM name")]) -> dict[str, object]:
    """List virtiofs shares configured for a VM (host directories shared into
    the guest) with the commands to mount them inside the guest."""
    return await _run(virtiofs.list_fileshares, name)


@mcp.tool(annotations=MUTATING)
async def add_fileshare(
    name: Annotated[str, Field(description="VM name (must be shut off)")],
    host_path: Annotated[str, Field(description="Host directory to share (created if missing)")],
    mount_tag: Annotated[
        str | None, Field(description="Tag used inside the guest (default: directory name)")
    ] = None,
    readonly: Annotated[bool, Field(description="Share read-only")] = False,
    accessmode: Annotated[
        Literal["passthrough", "mapped", "mapped-xattr"],
        Field(description="How file ownership is presented in the guest"),
    ] = "passthrough",
) -> dict[str, object]:
    """Share a host directory into a VM via virtiofs. Requires the VM to be
    shut off (shared memory backing is added to its config); takes effect on
    next boot. Mount inside the guest with the returned command."""
    return await _run(
        virtiofs.add_fileshare,
        name,
        host_path,
        mount_tag=mount_tag,
        readonly=readonly,
        accessmode=accessmode,
    )


@mcp.tool(annotations=MUTATING)
async def remove_fileshare(
    name: Annotated[str, Field(description="VM name (must be shut off)")],
    mount_tag: Annotated[str, Field(description="Mount tag of the share to remove")],
    keep_memory_backing: Annotated[
        bool, Field(description="Keep the shared memory config (needed by other vhost-user devices)")
    ] = False,
) -> dict[str, object]:
    """Remove a virtiofs share from a VM (the host directory is kept)."""
    return await _run(
        virtiofs.remove_fileshare, name, mount_tag, keep_memory_backing=keep_memory_backing
    )


def main() -> None:
    """Run the server over stdio (the transport used by MCP clients)."""
    mcp.run()


if __name__ == "__main__":
    main()

# libvirt-mcp

An [MCP](https://modelcontextprotocol.io) server for managing **libvirt/KVM virtual
machines** through the official Python bindings: start/stop VMs, snapshot and restore
them, clone and delete them, provision new VMs from cloud images with
`virt-customize`, run commands inside guests, and share host directories via virtiofs.

## Tools

| Area | Tools |
| --- | --- |
| Inventory | `get_host_info`, `list_vms`, `get_vm_info`, `list_images`, `list_recommended_images`, `download_image` |
| Lifecycle | `start_vm`, `stop_vm`, `reboot_vm`, `pause_vm`, `resume_vm`, `set_vm_autostart` |
| Snapshots | `list_snapshots`, `create_snapshot`, `restore_snapshot`, `delete_snapshot` |
| Create / clone / delete | `create_vm`, `clone_vm`, `delete_vm`, `customize_image` |
| In-guest commands | `exec_in_vm` (qemu guest agent, SSH fallback via `qemu:system/<vm>` proxy then IP, `force_ssh`) |
| File shares | `list_fileshares`, `add_fileshare`, `remove_fileshare` (virtiofs) |

Destructive operations (`delete_vm`, forced stop/reboot, `restore_snapshot`,
`delete_snapshot`) refuse to run without `confirm=true`.

## Prerequisites

Fedora/RHEL:

```bash
sudo dnf install libvirt qemu-kvm virtiofsd python3-libvirt libguestfs-tools-c qemu-img
sudo usermod -aG libvirt,kvm "$USER"   # then re-login
```

The libvirt bindings come from the distro (`python3-libvirt`) — they are ABI-coupled
to the system libvirt. The dedicated SSH key **must exist** (injected into created
VMs, used by the SSH fallback); creation fails fatally without it:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/vms
```

## Installation

```bash
cd libvirt-mcp
uv venv --system-site-packages --python "$(command -v python3)"
uv sync
uv run python -c "import libvirt, libvirt_qemu, libvirt_mcp; print('ok')"
```

## MCP client configuration

**OpenCode** — the project's `opencode.json` is ready; for another project use:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "libvirt": {
        "type": "local",
        "command": ["uv", "run", "libvirt-mcp"],
        "cwd": "/path/to/libvirt-mcp"
      }
    }
  }
}
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIBVIRT_MCP_URI` | `qemu:///system` | libvirt connection URI |
| `LIBVIRT_MCP_SSH_KEY` | `~/.ssh/vms` | Dedicated SSH key (must exist) |
| `LIBVIRT_MCP_IMAGES_DIR` | `~/VirtualMachines` | Where VM disks live / are created |
| `LIBVIRT_MCP_SSH_PORT` | `22` | SSH fallback port |
| `LIBVIRT_MCP_CUSTOMIZE_TIMEOUT` | `1800` | virt-customize timeout (s) |
| `LIBVIRT_MCP_EXEC_TIMEOUT` | `120` | in-guest command timeout (s) |
| `LIBVIRT_MCP_LOG_LEVEL` | `INFO` | stderr log level |

`opencode.json` sets `LIBVIRT_MCP_URI` and `LIBVIRT_MCP_SSH_KEY` explicitly —
edit them there (OpenCode restarts the server on save); `~` is expanded. With a
remote URI (`qemu+ssh://…`) the pure-libvirt tools work, but the disk-image
tooling (`create_vm`, `clone_vm`, `customize_image`, `delete_vm`) needs the
images on a filesystem this server can see.

## Typical usage

```
download_image(image_id="debian-13")
create_vm(name="build", base_image="debian-13-generic-amd64.qcow2",
          vcpus=4, memory_mb=4096, disk_gb=20, packages=["nginx"], update=true)
exec_in_vm(name="build", command="uname -a", ssh_user="root")
create_snapshot(name="build", snapshot_name="clean")
restore_snapshot(name="build", snapshot_name="clean", confirm=true)
stop_vm(name="build", wait=true)
add_fileshare(name="build", host_path="~/src")
exec_in_vm(name="build", command="mkdir -p /mnt/src && mount -t virtiofs src /mnt/src",
           ssh_user="root", force_ssh=true)
```

## Notes

- **`download_image`** always writes inside the images directory
  (`LIBVIRT_MCP_IMAGES_DIR`); the `filename` argument is stripped of any path
  parts, so it cannot escape that directory.
- **`create_vm`** copies the base image, runs `virt-customize` on the copy
  (injects the SSH key for root, sets the root password via a `0600` temp file so
  it never hits the process list, installs packages), defines and starts the
  domain, and cleans up after itself on failure. `qemu-guest-agent` is
  installed/enabled when missing, and `guest-exec` is added to the RPC allowlist
  that RHEL/Rocky 10 use to harden the agent. UEFI-only images (e.g. Fedora UKI,
  RHEL 10) need `firmware="efi"`. **The base must be a pristine cloud image**:
  a disk that already belongs to a VM (`used_by` in `list_images`) carries that
  guest's state — e.g. an fstab entry for a virtiofs share that new VMs don't
  have — and fresh VMs from it hang in an emergency shell with no agent, network
  or sshd.
- **`exec_in_vm`** prefers the guest agent and falls back to SSH. The SSH
  username is a **required** `ssh_user` argument — there is no default, and an
  error is raised if the user cannot connect (auth refused, unknown user,
  unreachable). `create_vm` injects the key for `root`, so `ssh_user="root"`
  works out of the box; for other users, inject their key yourself (e.g. with
  `run_commands` in `create_vm`). Note: the agent path always runs commands as
  root in the guest — the agent protocol has no user concept; use
  `force_ssh=true` with a non-root `ssh_user` to avoid that. On
  SELinux-hardened guests (RHEL/Rocky 10+) agent commands also run in a
  confined context — `force_ssh=true` for privileged operations like mounting.
- **virtiofs shares** rewrite the domain XML (shared memfd memory + filesystem
  device); the VM must be shut off when adding/removing, and snapshots do not
  cover the shared directory.
- **Snapshots**: full disk+memory by default; `disk_only=true` for external
  snapshots. Quiescing applies to `disk_only` only.

## Troubleshooting

- **`libvirt python bindings were not found`** — install `python3-libvirt` and
  create the venv with `--system-site-packages`.
- **`virt-customize` slow/failing** — needs `/dev/kvm` (`kvm` group) and network
  access for `--install`/`--update`.
- **VM with a virtiofs share fails to start** — install `virtiofsd`.
- **UEFI image won't boot** — pass `firmware="efi"` to `create_vm`.
- **Logs** — stderr; set `LIBVIRT_MCP_LOG_LEVEL=DEBUG` for details.

## Development

```bash
uv run python scripts/e2e_demo.py # full workflow against the real hypervisor
```

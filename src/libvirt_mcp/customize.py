"""virt-customize wrapper and the create_vm workflow.

Keep it simple: one dedicated SSH keypair (~/.ssh/vms) injected for root, one
code path for customization. If the key is missing, everything fails fast.
"""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
import time

from libvirt_mcp import images, xmlutil
from libvirt_mcp.config import get_config
from libvirt_mcp.connection import get_conn
from libvirt_mcp.util import LibvirtMcpError, require_ssh_keypair, tail

_libvirt = None

#: Install qemu-guest-agent only when missing from the image. RHEL/Rocky/
#: Fedora (and most) cloud images ship it already, which keeps create_vm
#: network-free for them. Best effort: exec_in_vm falls back to SSH otherwise.
_INSTALL_AGENT_IF_MISSING = (
    "if [ ! -x /usr/bin/qemu-ga ] && [ ! -x /usr/sbin/qemu-ga ]; then "
    "command -v dnf >/dev/null 2>&1 && dnf -y install qemu-guest-agent || "
    "command -v yum >/dev/null 2>&1 && yum -y install qemu-guest-agent || "
    "command -v apt-get >/dev/null 2>&1 && apt-get update && apt-get -y install qemu-guest-agent || "
    "command -v zypper >/dev/null 2>&1 && zypper --non-interactive install qemu-guest-agent || "
    "echo 'warning: could not install qemu-guest-agent' >&2; "
    "fi"
)

#: RHEL 10 / Rocky 10 / Fedora harden qemu-guest-agent with an RPC allowlist
#: (/etc/sysconfig/qemu-ga) that excludes guest-exec, which exec_in_vm needs.
#: Append guest-exec(+status) to the allowlist when present; no-op otherwise.
#: NOTE: the "already applied" check must be anchored to the active
#: FILTER_RPC_ARGS line — the file contains a commented example mentioning
#: guest-exec, which would otherwise defeat the guard.
_ENABLE_GUEST_EXEC = (
    "f=/etc/sysconfig/qemu-ga; "
    "[ -f \"$f\" ] && grep -q '^FILTER_RPC_ARGS=\"--allow-rpcs' \"$f\" "
    "&& ! grep -q '^FILTER_RPC_ARGS=.*guest-exec' \"$f\" "
    "&& sed -i '/^FILTER_RPC_ARGS=\"--allow-rpcs/ s/\"$/,guest-exec,guest-exec-status\"/' \"$f\" || true"
)


def _lv():
    global _libvirt
    if _libvirt is None:
        from libvirt_mcp import compat

        _libvirt = compat.libvirt
    return _libvirt


# --------------------------------------------------------------------------
# virt-customize (argument building is pure, for unit tests)
# --------------------------------------------------------------------------


def build_customize_args(
    image,
    *,
    hostname: str | None = None,
    ssh_pubkey: str,
    packages: list[str] | None = None,
    update: bool = False,
    run_commands: list[str] | None = None,
) -> list[str]:
    args = [
        "virt-customize",
        "-a",
        str(image),
        "--no-logfile",
        "--memsize",
        "1024",
        "--ssh-inject",
        f"root:file:{ssh_pubkey}",
    ]
    if hostname:
        args += ["--hostname", hostname]
    if packages:
        args += ["--install", ",".join(packages)]
    if update:
        args += ["--update"]
    for cmd in run_commands or []:
        args += ["--run-command", cmd]
    return args


def _run_virt_customize(args: list[str], timeout: int) -> str:
    env = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}
    try:
        cp = subprocess.run(
            [str(a) for a in args], capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as e:
        raise LibvirtMcpError(
            f"virt-customize timed out after {timeout}s "
            "(package installs can be slow; raise the timeout if needed)"
        ) from e
    except FileNotFoundError as e:
        raise LibvirtMcpError(
            "virt-customize not found; install libguestfs-tools-c (Fedora/RHEL) "
            "or libguestfs-tools (Debian/Ubuntu)"
        ) from e
    output = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        raise LibvirtMcpError(
            f"virt-customize failed (exit {cp.returncode}):\n{tail(output, 3000)}"
        )
    return tail(output, 2000)


def customize_disk(
    path,
    *,
    hostname: str | None = None,
    root_password: str | None = None,
    packages: list[str] | None = None,
    update: bool = False,
    run_commands: list[str] | None = None,
    timeout: int | None = None,
) -> str:
    """Run virt-customize on a disk image; returns the log tail.

    The root password (if any) goes through a 0600 temp file so it never
    shows up in the process list. Fails fatally if the SSH keypair is missing.
    """

    cfg = get_config()
    ssh_pubkey = require_ssh_keypair(cfg.ssh_key)  # fatal if missing

    args = build_customize_args(
        path,
        hostname=hostname,
        ssh_pubkey=str(ssh_pubkey),
        packages=packages,
        update=update,
        run_commands=run_commands,
    )
    password_file = None
    try:
        if root_password:
            fd, password_file = tempfile.mkstemp(prefix="lvmcp-pw-", text=True)
            os.write(fd, root_password.encode())
            os.close(fd)
            os.chmod(password_file, 0o600)
            args += ["--root-password", f"file:{password_file}"]
        return _run_virt_customize(args, timeout or cfg.customize_timeout)
    finally:
        if password_file:
            try:
                os.unlink(password_file)
            except OSError:
                pass


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------


def check_image_not_in_use(image_path) -> list[str]:
    """Raise if a RUNNING domain uses this image; return shutoff users (warning)."""

    conn = get_conn()
    running_users, shutoff_users = [], []
    for dom in conn.listAllDomains(0):
        root = xmlutil.parse(dom.XMLDesc(0)).getroot()
        if image_path in xmlutil.file_disk_paths(root):
            if dom.state()[0] == _lv().VIR_DOMAIN_RUNNING:
                running_users.append(dom.name())
            else:
                shutoff_users.append(dom.name())
    if running_users:
        raise LibvirtMcpError(
            f"VM(s) {', '.join(running_users)} are RUNNING and use {image_path}; "
            "stop them before customizing the image (customizing a live disk corrupts it)"
        )
    return shutoff_users


def _assert_name_free(name: str) -> None:
    conn = get_conn()
    try:
        conn.lookupByName(name)
        raise LibvirtMcpError(f"a VM named '{name}' already exists")
    except _lv().libvirtError as e:
        if e.get_error_code() != _lv().VIR_ERR_NO_DOMAIN:
            raise


# --------------------------------------------------------------------------
# high-level operations
# --------------------------------------------------------------------------


def customize_image(
    image: str,
    *,
    root_password: str | None = None,
    packages: list[str] | None = None,
    update: bool = False,
    run_commands: list[str] | None = None,
) -> dict:
    """Run virt-customize on an existing disk image (any VM using it must be off)."""

    cfg = get_config()
    path = images.resolve_image_path(image)
    shutoff_users = check_image_not_in_use(str(path.resolve()))

    log_tail = customize_disk(
        path,
        root_password=root_password,
        packages=packages,
        update=update,
        run_commands=run_commands,
    )
    result = {
        "image": str(path),
        "root_password_set": bool(root_password),
        "ssh_key_injected": str(cfg.ssh_key) + ".pub",
        "packages": packages or [],
        "update": update,
        "run_commands": run_commands or [],
        "log_tail": log_tail,
        "note": "image customized in place; start the VM to use it",
    }
    if shutoff_users:
        result["warning"] = (
            f"image is also used by shut-off VM(s) {', '.join(shutoff_users)}; "
            "they will pick up these changes on next boot"
        )
    return result


def create_vm(
    name: str,
    base_image: str,
    *,
    vcpus: int = 2,
    memory_mb: int = 2048,
    disk_gb: float | None = None,
    root_password: str | None = None,
    packages: list[str] | None = None,
    update: bool = False,
    run_commands: list[str] | None = None,
    network: str = "default",
    firmware: str = "bios",
    start: bool = True,
) -> dict:
    """Create a VM from a base cloud image:

    1. fail fast if the name is taken or the SSH keypair is missing
    2. copy the base image to <images_dir>/<name>.qcow2 (grow it if disk_gb)
    3. virt-customize the copy: inject ~/.ssh/vms.pub for root, set the root
       password, install packages (qemu-guest-agent always), update, run commands
    4. define the domain and start it
    """

    cfg = get_config()
    xmlutil.validate_vm_name(name)
    _assert_name_free(name)
    ssh_pubkey = require_ssh_keypair(cfg.ssh_key)  # fatal if missing

    # 1. disk: copy base image (clean up the partial VM if anything fails)
    base = images.resolve_image_path(base_image)
    dest = cfg.images_dir / f"{name}.qcow2"
    if dest.exists():
        raise LibvirtMcpError(
            f"disk image already exists: {dest} (was this VM name already used?)"
        )
    cfg.images_dir.mkdir(parents=True, exist_ok=True)
    dom = None
    try:
        images.convert_image(base, dest, "qcow2")
        if disk_gb:
            images.resize_image(dest, int(disk_gb * 1024**3))

        # 2. customize the copy (ensure the guest agent is installed, enabled
        #    and allowed to run commands so exec_in_vm works)
        cmds = list(run_commands or [])
        cmds.append(_INSTALL_AGENT_IF_MISSING)
        cmds.append("systemctl enable qemu-guest-agent.service || true")
        cmds.append(_ENABLE_GUEST_EXEC)
        # A base image that already booted once (e.g. a retired VM's disk)
        # carries cloud-init state and network profiles bound to the OLD MAC;
        # without this, the new VM's random MAC never gets configured and the
        # boot stalls on network. Reset so first boot re-runs per-instance setup.
        cmds.append("command -v cloud-init >/dev/null 2>&1 && cloud-init clean --logs || true")
        log_tail = customize_disk(
            dest,
            hostname=name,
            root_password=root_password,
            packages=packages,
            update=update,
            run_commands=cmds,
        )

        # 3. define and optionally start
        conn = get_conn()
        want_network = bool(network) and network.lower() != "none"
        if want_network:
            try:
                conn.networkLookupByName(network)
            except _lv().libvirtError:
                raise LibvirtMcpError(
                    f"libvirt network '{network}' does not exist (see get_host_info); "
                    "pass network='none' for a VM without networking"
                ) from None
        xml = xmlutil.build_domain_xml(
            name=name,
            memory_mib=memory_mb,
            vcpus=vcpus,
            disk_path=str(dest),
            arch=platform.machine(),
            network=network if want_network else None,
            firmware=firmware,
            description=f"Created by libvirt-mcp at {xmlutil.iso_utc(time.time())}",
        )
        dom = conn.defineXML(xml)
        if start:
            dom.create()
    except BaseException:
        # leave no half-created VM behind: undefine the domain and drop the disk
        if dom is not None:
            try:
                dom.undefine()
            except Exception:
                pass
        try:
            if dest.exists():
                os.unlink(dest)
        except OSError:
            pass
        raise

    return {
        "name": name,
        "state": "running" if start else "shut off",
        "uuid": dom.UUIDString(),
        "base_image": str(base),
        "disk_path": str(dest),
        "disk_gb": disk_gb,
        "vcpus": vcpus,
        "memory_mb": memory_mb,
        "network": network,
        "ssh_key_injected": str(ssh_pubkey),
        "packages": packages or [],
        "customize_log_tail": log_tail,
        "note": (
            "no root password was set unless root_password was given (cloud images "
            "disable password logins anyway); use exec_in_vm to run commands in the VM"
            if not root_password
            else "root password was set as requested"
        ),
    }

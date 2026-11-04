"""Disk image inventory and manipulation (qemu-img, downloads)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from libvirt_mcp import xmlutil
from libvirt_mcp.connection import get_conn
from libvirt_mcp.config import get_config
from libvirt_mcp.util import LibvirtMcpError, human_bytes, run_cmd

IMAGE_SUFFIXES = {".qcow2", ".img", ".raw", ".iso"}

#: Curated, verified cloud images (amd64). Any URL can also be passed to
#: download_image directly.
RECOMMENDED_IMAGES = [
    {
        "id": "ubuntu-24.04",
        "name": "Ubuntu 24.04 LTS (Noble) server cloud image",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "format": "qcow2",
    },
    {
        "id": "ubuntu-22.04",
        "name": "Ubuntu 22.04 LTS (Jammy) server cloud image",
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        "format": "qcow2",
    },
    {
        "id": "debian-13",
        "name": "Debian 13 (Trixie) generic cloud image",
        "url": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2",
        "format": "qcow2",
    },
    {
        "id": "debian-12",
        "name": "Debian 12 (Bookworm) generic cloud image",
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
        "format": "qcow2",
    },
    {
        "id": "fedora-43",
        "name": "Fedora Cloud 43 (Generic) base image",
        "url": "https://dl.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2",
        "format": "qcow2",
    },
    {
        "id": "fedora-44",
        "name": "Fedora Cloud 44 (Generic) base image",
        "url": "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
        "format": "qcow2",
    },
    {
        "id": "rocky-10",
        "name": "Rocky Linux 10 GenericCloud base image",
        "url": "https://dl.rockylinux.org/pub/rocky/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2",
        "format": "qcow2",
    },
    {
        "id": "almalinux-10",
        "name": "AlmaLinux 10 GenericCloud base image",
        "url": "https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2",
        "format": "qcow2",
    },
]


def qemu_img_info(path) -> dict:
    """`qemu-img info` as a dict. Uses --force-share: safe on in-use images."""

    cp = run_cmd(
        ["qemu-img", "info", "--output=json", "--force-share", str(path)],
        timeout=60,
        what=f"inspecting image {path}",
    )
    return json.loads(cp.stdout)


def image_usage_map() -> dict[str, list[str]]:
    """Map of absolute disk path -> VM names currently referencing it."""

    usage: dict[str, list[str]] = {}
    for dom in get_conn().listAllDomains(0):
        for path in xmlutil.file_disk_paths(xmlutil.parse(dom.XMLDesc(0)).getroot()):
            usage.setdefault(path, []).append(dom.name())
    return usage


def list_images() -> list[dict]:
    cfg = get_config()
    if not cfg.images_dir.is_dir():
        return []
    usage = image_usage_map()

    out = []
    for path in sorted(cfg.images_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        entry = {
            "name": path.name,
            "path": str(path),
            "used_by": usage.get(str(path.resolve()), usage.get(str(path), [])),
        }
        try:
            info = qemu_img_info(path)
            entry["format"] = info.get("format")
            entry["virtual_size"] = human_bytes(info.get("virtual-size"))
            entry["size_on_disk"] = human_bytes(info.get("actual-size"))
        except LibvirtMcpError as e:
            entry["format"] = "unknown"
            entry["note"] = str(e)
        out.append(entry)
    return out


def list_recommended_images() -> list[dict]:
    return RECOMMENDED_IMAGES


def convert_image(src, dst, output_format: str = "qcow2", timeout: int | None = None, what: str | None = None) -> None:
    """Copy a disk image into a new, independent image (sparse-aware)."""

    run_cmd(
        ["qemu-img", "convert", "-O", output_format, str(src), str(dst)],
        timeout=timeout or 3600,
        what=what or f"copying {src} -> {dst}",
    )


def resize_image(path, size_bytes: int, timeout: int = 120) -> None:
    info = qemu_img_info(path)
    current = info.get("virtual-size", 0)
    if size_bytes < current:
        raise LibvirtMcpError(
            f"cannot shrink disk {path}: requested {human_bytes(size_bytes)} is smaller "
            f"than the current virtual size {human_bytes(current)}"
        )
    if size_bytes == current:
        return
    run_cmd(
        ["qemu-img", "resize", str(path), str(size_bytes)],
        timeout=timeout,
        what=f"resizing {path}",
    )


def safe_dest_name(filename: str | None, url: str) -> str:
    """Destination filename for a download: user input or URL basename, with
    any directory parts stripped so it cannot escape the images directory.
    "." and ".." resolve to empty, which download_image rejects."""

    raw = filename or url.rstrip("/").rsplit("/", 1)[-1]
    name = Path(raw).name
    return "" if name in (".", "..") else name


def download_image(image_id: str | None = None, url: str | None = None, filename: str | None = None) -> dict:
    """Download a cloud image into the images directory (via curl)."""

    cfg = get_config()
    if bool(image_id) == bool(url):
        raise LibvirtMcpError("pass exactly one of image_id (from list_recommended_images) or url")
    if image_id:
        match = [i for i in RECOMMENDED_IMAGES if i["id"] == image_id]
        if not match:
            raise LibvirtMcpError(
                f"unknown image id '{image_id}'; call list_recommended_images for the list"
            )
        url = match[0]["url"]
    dest_name = safe_dest_name(filename, url)
    if not dest_name:
        raise LibvirtMcpError("could not derive a filename from the URL; pass filename=")
    dest = cfg.images_dir / dest_name
    if dest.exists():
        raise LibvirtMcpError(f"{dest} already exists; refusing to overwrite")
    cfg.images_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    run_cmd(
        ["curl", "-L", "--fail", "--retry", "3", "-o", str(tmp), url],
        timeout=3600,
        what=f"downloading {url}",
    )
    try:
        info = qemu_img_info(tmp)
    except LibvirtMcpError as e:
        tmp.unlink(missing_ok=True)
        raise LibvirtMcpError(
            f"downloaded file is not a usable disk image: {e}"
        ) from e
    os.replace(tmp, dest)
    return {
        "path": str(dest),
        "format": info.get("format"),
        "virtual_size": human_bytes(info.get("virtual-size")),
        "size_on_disk": human_bytes(info.get("actual-size")),
        "source_url": url,
    }


def resolve_image_path(image: str) -> Path:
    """Resolve a user-supplied image reference (absolute path or name in images dir)."""

    cfg = get_config()
    path = Path(image).expanduser()
    if not path.is_absolute():
        path = cfg.images_dir / path
    if not path.is_file():
        raise LibvirtMcpError(
            f"image not found: {path} (see list_images / list_recommended_images + download_image)"
        )
    return path

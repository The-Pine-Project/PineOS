#!/usr/bin/env python3
"""
PineOS Installer — built on top of archinstall.
Requires archinstall >= 2.7 (python-archinstall in Arch repos).

Run via: sudo python /usr/local/bin/PineOS-Installer.py
"""

import os
import sys
import getpass
import shutil
import subprocess
import urllib.request
import urllib.error
import json
from pathlib import Path

# ── Sanity checks ──────────────────────────────────────────────────────────────

if os.geteuid() != 0:
    sys.exit("Error: This installer must be run as root (sudo).")

try:
    import archinstall

    # archinstall 3.x moved Installer out of __init__.py
    try:
        from archinstall.lib.installer import Installer          # 3.x
    except ImportError:
        from archinstall import Installer                        # 2.x fallback

    from archinstall.lib.disk.device_handler import device_handler
    from archinstall.lib.disk.filesystem import FilesystemHandler

    # All disk-model imports are in archinstall.lib.models.device (archinstall 3.x+)
    from archinstall.lib.models.device import (
        DiskLayoutConfiguration,
        DiskLayoutType,
        DeviceModification,
        PartitionModification,
        FilesystemType,
        PartitionType,
        PartitionFlag,
        ModificationStatus,
        Size,
        Unit,
    )

    # Network configuration
    try:
        from archinstall.lib.models.network import (
            NetworkConfiguration,
            NicType,
        )
    except ImportError:
        from archinstall.lib.network_configuration import (
            NetworkConfiguration,
            NicType,
        )

    # User model
    from archinstall.lib.models import User
    try:
        from archinstall.lib.models.users import Password
    except ImportError:
        from archinstall.lib.models import Password

    # Locale model
    from archinstall.lib.models.locale import LocaleConfiguration

    # Bootloader model — kept for potential future use; not used for GRUB install
    # (we run grub-install directly via arch_chroot instead)
    try:
        from archinstall.lib.models.bootloader import Bootloader
    except ImportError:
        Bootloader = None

except ImportError as exc:
    sys.exit(
        f"Error: archinstall is not installed or is too old.\n"
        f"  Install it with: pacman -S python-archinstall\n"
        f"  Details: {exc}"
    )

# ── Enum validation ────────────────────────────────────────────────────────────

def _validate_enums() -> None:
    """
    Verify every archinstall enum member used in this installer exists in the
    installed version of the library.  Fails immediately with a clear message
    instead of crashing deep inside the installation process.
    """
    required = {
        "ModificationStatus": (ModificationStatus, ["CREATE"]),
        "PartitionType":      (PartitionType,      ["Primary"]),
        "FilesystemType":     (FilesystemType,      ["FAT32", "LINUX_SWAP", "EXT4"]),
        "PartitionFlag":      (PartitionFlag,       ["BOOT"]),
        "DiskLayoutType":     (DiskLayoutType,      ["Default"]),
    }
    missing = []
    for cls_name, (cls, members) in required.items():
        for member in members:
            if not hasattr(cls, member):
                available = [e.name for e in cls]
                missing.append(
                    f"  {cls_name}.{member} — not found. "
                    f"Available: {', '.join(available)}"
                )
    if missing:
        sys.exit(
            "Error: archinstall enum mismatch — the installed version of "
            "python-archinstall is incompatible with this installer.\n"
            "Mismatches:\n" + "\n".join(missing) + "\n\n"
            "Try: pacman -Syu python-archinstall"
        )

_validate_enums()

# ── Constants ──────────────────────────────────────────────────────────────────

VERSION      = "PineOS-v0.2.3"
MOUNTPOINT   = Path("/mnt/pineos-install")
MOTD_TEXT    = (
    "╔══════════════════════════════════════╗\n"
    "║          Welcome to PineOS!          ║\n"
    "║  https://github.com/The-Pine-Project ║\n"
    "╚══════════════════════════════════════╝\n"
)

# Remote version manifest — JSON file with {"version": "PineOS-vX.Y.Z", "changelog": "..."}
VERSION_MANIFEST_URL = (
    "https://github.com/The-Pine-Project/PineOS/raw/refs/heads/main/version.json"
)

# Per-desktop install scripts hosted in the PineOS repo.
# Each script is a newline-separated list of pacman package names (comments
# starting with # are ignored).  Fetched at install time so the package list
# is always up-to-date without requiring a new installer ISO.
DESKTOP_SCRIPT_URLS = {
    "gnome": "https://github.com/The-Pine-Project/PineOS/raw/refs/heads/main/desktops/gnome-install.sh",
    "kde":   "https://github.com/The-Pine-Project/PineOS/raw/refs/heads/main/desktops/kde-install.sh",
    "xfce":  "https://github.com/The-Pine-Project/PineOS/raw/refs/heads/main/desktops/xfce-install.sh",
}

# Desktop environment package sets
DESKTOP_ENVIRONMENTS = {
    "none":  {
        "label": "No desktop (minimal CLI)",
        "pkgs":  [],
        "services": [],
    },
    "gnome": {
        "label": "GNOME",
        "pkgs":  ["gnome", "gnome-tweaks", "gdm"],
        "services": ["gdm"],
    },
    "kde": {
        "label": "KDE Plasma",
        "pkgs":  ["plasma", "plasma-wayland-session", "kde-applications", "sddm"],
        "services": ["sddm"],
    },
    "xfce": {
        "label": "XFCE",
        "pkgs":  ["xfce4", "xfce4-goodies", "lightdm", "lightdm-gtk-greeter"],
        "services": ["lightdm"],
    },
}

COMMON_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Australia/Sydney",
]

# ── PineOS repositories ────────────────────────────────────────────────────────

# These repo blocks are injected into the target's /etc/pacman.conf after the
# base system is installed.  SigLevel is set to Optional because the PineOS
# repo signing key may not be in the Arch keyring on a fresh install.
PINEOS_REPOS = {
    "pineos-stable": {
        "label": "Stable  — tested, production-ready packages",
        "repo_block": (
            "\n[pineos-stable]\n"
            "SigLevel = Optional TrustAll\n"
            "Server = https://github.com/The-Pine-Project/pkg-stable-repo/raw/refs/heads/main/$arch\n"
        ),
        "pkg": [
            "pkg-edge",     # PineOS package managers
            "pkg-stable",
        ],
    },
}

# There is currently only one channel — loaded directly without user selection.
PINEOS_CHANNEL = PINEOS_REPOS["pineos-stable"]


GREEN  = "\033[0;32m"
YELLOW = "\033[0;33m"
RED    = "\033[0;31m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def header(text: str) -> None:
    width = len(text) + 6
    print(f"\n{GREEN}{'─' * width}{RESET}")
    print(f"{GREEN}   {BOLD}{text}{RESET}")
    print(f"{GREEN}{'─' * width}{RESET}")


def info(text: str) -> None:
    print(f"  {GREEN}•{RESET} {text}")


def warn(text: str) -> None:
    print(f"  {YELLOW}⚠{RESET}  {text}")


def error(text: str) -> None:
    print(f"  {RED}✗{RESET}  {text}", file=sys.stderr)


def prompt(text: str, default: str = "") -> str:
    """Prompt with an optional default value shown in brackets."""
    hint = f" [{default}]" if default else ""
    return input(f"  {text}{hint}: ").strip() or default


def confirm(text: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"  {text} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def pick_from_list(items: list, label_fn=str, title: str = "Options") -> int:
    """Display a numbered list and return the chosen index."""
    print()
    for i, item in enumerate(items):
        print(f"    [{i}] {label_fn(item)}")
    while True:
        try:
            idx = int(input(f"\n  Select {title} number: "))
            if 0 <= idx < len(items):
                return idx
        except ValueError:
            pass
        warn("Invalid selection, try again.")


# ── Step 1 — Disk ──────────────────────────────────────────────────────────────

def pick_disk():
    """Let the user choose an installation target disk."""
    devices = device_handler.devices
    if not devices:
        sys.exit("Error: No disks detected. Aborting.")

    header("Disk selection")

    def dev_label(dev):
        size = dev.device_info.total_size.format_highest()
        return f"{dev.device_info.path}  ({size})"

    idx = pick_from_list(devices, label_fn=dev_label, title="disk")
    return devices[idx]


def get_swap_size() -> int:
    """Ask for a swap partition size in MiB (0 to disable)."""
    header("Swap")
    ram_gb_actual = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    # /proc/meminfo reports kibibytes; convert to GiB
                    ram_gb_actual = int(line.split()[1]) // 1024 // 1024
                    break
    except OSError:
        pass

    # FIX #4: Separate actual RAM display from the swap suggestion.
    # Suggest swap = RAM + 1 GiB (gives headroom for hibernation).
    suggestion = ram_gb_actual + 1 if ram_gb_actual else 4
    info(f"Detected ~{ram_gb_actual} GiB RAM — suggested swap: {suggestion} GiB")
    raw = prompt("Swap size in GiB (0 to disable)", str(suggestion))
    try:
        return max(0, int(raw)) * 1024
    except ValueError:
        warn("Invalid input — disabling swap.")
        return 0


# ── Step 2 — Credentials & settings ───────────────────────────────────────────

def _collect_one_user(is_first: bool) -> dict:
    """Collect username, password, and sudo flag for a single user."""
    if is_first:
        info("This will be your primary admin account (sudo enabled).")
    username = prompt("Username")
    while not username:
        warn("Username cannot be empty.")
        username = prompt("Username")

    while True:
        password = getpass.getpass(f"  Password for {username}: ")
        if not password:
            warn("Password cannot be empty.")
            continue
        confirm_pw = getpass.getpass("  Confirm password: ")
        if password == confirm_pw:
            break
        warn("Passwords do not match, try again.")

    sudo = is_first or confirm(f"Give {username} sudo (admin) privileges?", default=False)
    return {"username": username, "password": password, "sudo": sudo}


def get_credentials() -> tuple[str, list[dict], str]:
    """Collect hostname, one or more users, and root password."""
    header("User & system configuration")

    hostname = prompt("Hostname", "pineos")

    users = []
    info("Add at least one user account.")
    while True:
        users.append(_collect_one_user(is_first=(len(users) == 0)))
        info(f"User '{users[-1]['username']}' added  "
             f"({'sudo' if users[-1]['sudo'] else 'standard'}).")
        if not confirm("Add another user?", default=False):
            break

    if confirm("Set a separate root password?", default=False):
        while True:
            root_pw = getpass.getpass("  Root password: ")
            if not root_pw:
                warn("Password cannot be empty.")
                continue
            confirm_rp = getpass.getpass("  Confirm root password: ")
            if root_pw == confirm_rp:
                break
            warn("Passwords do not match, try again.")
    else:
        root_pw = users[0]["password"]
        info("Root password will match primary user password.")

    return hostname, users, root_pw


# ── Version check ──────────────────────────────────────────────────────────────

def check_for_updates() -> None:
    """
    Fetch the remote version manifest and offer to pull new installer files
    if a newer version is available.
    """
    header("Checking for updates")
    info(f"Installed version: {VERSION}")
    try:
        with urllib.request.urlopen(VERSION_MANIFEST_URL, timeout=5) as resp:
            manifest = json.loads(resp.read().decode())
        remote_version = manifest.get("version", "")
        changelog      = manifest.get("changelog", "No changelog provided.")
    except Exception as e:
        warn(f"Could not reach update server ({e}). Continuing with installed version.")
        return

    if not remote_version or remote_version == VERSION:
        info("You are running the latest version.")
        return

    print()
    warn(f"A newer version is available: {remote_version}")
    print(f"\n  Changelog:\n  {changelog}\n")
    if confirm("Fetch updated installer files before continuing?", default=True):
        _fetch_updated_installer(remote_version, manifest)
    else:
        info("Skipping update — continuing with current version.")


def _fetch_updated_installer(remote_version: str, manifest: dict) -> None:
    """Download updated installer script and re-exec if the fetch succeeds."""
    installer_url = manifest.get("installer_url", "")
    if not installer_url:
        warn("No installer_url in manifest — cannot auto-update.")
        return

    tmp = Path("/tmp/PineOS-Installer-new.py")
    try:
        info(f"Downloading {remote_version} installer…")
        urllib.request.urlretrieve(installer_url, str(tmp))
        tmp.chmod(0o755)
        info("Download complete — restarting installer with new version.")
        os.execv(sys.executable, [sys.executable, str(tmp)] + sys.argv[1:])
    except Exception as e:
        warn(f"Update download failed: {e}. Continuing with current version.")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ── Desktop package list fetch ─────────────────────────────────────────────────

def fetch_desktop_packages(desktop_key: str, fallback_pkgs: list[str]) -> list[str]:
    """
    Fetch the remote install script for the chosen desktop and parse its
    package list.  Falls back to the hard-coded list if the fetch fails.

    Script format (same as a shell script — packages are bare words, lines
    starting with # are comments, blank lines are ignored):

        # GNOME packages for PineOS
        gnome
        gnome-tweaks
        gdm
        ...
    """
    url = DESKTOP_SCRIPT_URLS.get(desktop_key)
    if not url:
        return fallback_pkgs   # "none" or unknown key

    info(f"Fetching up-to-date package list for {desktop_key.upper()}…")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = resp.read().decode()
    except Exception as e:
        warn(f"Could not fetch desktop script ({e}). Using built-in package list.")
        return fallback_pkgs

    pkgs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Skip shell syntax lines (variable assignments, pacman -S calls, etc.)
        if any(c in line for c in ("=", "(", ")", ";", "&", "|", "$")):
            continue
        pkgs.append(line)

    if not pkgs:
        warn("Remote script returned no packages. Using built-in package list.")
        return fallback_pkgs

    info(f"Remote script: {len(pkgs)} packages.")
    return pkgs


# ── Step 3 — Locale & timezone ─────────────────────────────────────────────────

def get_locale_settings() -> tuple[str, str]:
    """Choose timezone and keyboard layout."""
    header("Locale & timezone")

    tz_idx = pick_from_list(
        COMMON_TIMEZONES,
        label_fn=lambda x: x,
        title="timezone",
    )
    timezone = COMMON_TIMEZONES[tz_idx]

    kb_layout = prompt("Keyboard layout", "us")

    return timezone, kb_layout


# ── Step 4 — Desktop environment ───────────────────────────────────────────────

def get_desktop() -> dict:
    """Let the user choose a desktop environment, then fetch its package list."""
    header("Desktop environment")

    keys   = list(DESKTOP_ENVIRONMENTS.keys())
    labels = [DESKTOP_ENVIRONMENTS[k]["label"] for k in keys]
    idx    = pick_from_list(labels, label_fn=str, title="desktop environment")
    chosen = keys[idx]
    de     = dict(DESKTOP_ENVIRONMENTS[chosen])   # shallow copy so we can mutate pkgs
    info(f"Selected: {de['label']}")

    # Fetch the up-to-date package list from the remote install script.
    # The returned list replaces the built-in fallback if the fetch succeeds.
    de["pkgs"] = fetch_desktop_packages(chosen, de["pkgs"])

    return de


# ── Step 5 — Extra packages ────────────────────────────────────────────────────

def get_extra_packages() -> list[str]:
    """Optionally add extra packages."""
    header("Extra packages")
    info("Enter additional packages to install, separated by spaces.")
    info("Leave blank to skip.")
    raw = input("  Extra packages: ").strip()
    pkgs = raw.split() if raw else []
    if pkgs:
        info(f"Will install: {', '.join(pkgs)}")
    return pkgs


# ── Step 6 — Review & confirm ──────────────────────────────────────────────────

def review_and_confirm(
    disk,
    hostname: str,
    users: list[dict],
    timezone: str,
    kb_layout: str,
    swap_mib: int,
    desktop: dict,
    extra_pkgs: list[str],
) -> None:
    header("Installation summary")
    print()
    info(f"Disk        : {disk.device_info.path}  ({disk.device_info.total_size.format_highest()})")
    info(f"Partitions  : EFI 512 MiB | {'Swap %d GiB | ' % (swap_mib // 1024) if swap_mib else ''}root (remainder, ext4)")
    info(f"Hostname    : {hostname}")
    for u in users:
        role = "sudo" if u["sudo"] else "standard"
        info(f"User        : {u['username']}  ({role})")
    info(f"Timezone    : {timezone}")
    info(f"Keyboard    : {kb_layout}")
    info(f"Desktop     : {desktop['label']}")
    info(f"Channel     : {PINEOS_CHANNEL['label'].strip()}")
    if extra_pkgs:
        info(f"Extra pkgs  : {', '.join(extra_pkgs)}")
    print()
    warn("ALL DATA ON THE SELECTED DISK WILL BE PERMANENTLY ERASED.")
    if not confirm("Proceed with installation?", default=False):
        sys.exit("Installation cancelled.")


# ── Disk layout builder ────────────────────────────────────────────────────────

def build_disk_layout(disk, swap_mib: int) -> DiskLayoutConfiguration:
    """Construct the GPT partition layout for the target disk."""
    sector = disk.device_info.sector_size
    device_mod = DeviceModification(disk, wipe=True)

    offset = 1          # MiB — leave 1 MiB at start for alignment

    # EFI partition
    # EFI partition — mounted at /boot so archinstall's GRUB installer can
    # detect it via find_partition_by_flag.  Mounting at /boot/efi causes
    # "Could not detect efi partition" because archinstall walks the disk
    # config looking for the ESP mountpoint and only recognises /boot.
    efi = PartitionModification(
        status=ModificationStatus.CREATE,
        type=PartitionType.Primary,
        start=Size(offset, Unit.MiB, sector),
        length=Size(512, Unit.MiB, sector),
        mountpoint=Path("/boot"),
        fs_type=FilesystemType.FAT32,
        flags=[PartitionFlag.BOOT],
    )
    device_mod.add_partition(efi)
    offset += 512

    # Optional swap partition
    if swap_mib > 0:
        swap = PartitionModification(
            status=ModificationStatus.CREATE,
            type=PartitionType.Primary,
            start=Size(offset, Unit.MiB, sector),
            length=Size(swap_mib, Unit.MiB, sector),
            mountpoint=None,
            fs_type=FilesystemType.LINUX_SWAP,
        )
        device_mod.add_partition(swap)
        offset += swap_mib

    # FIX #1: Root partition — calculate remaining MiB rather than using
    # Unit.Percent(100) as a *length*, which would overflow the disk.
    # Leave 1 MiB at the end for GPT backup header alignment.
    total_mib = int(disk.device_info.total_size.convert(Unit.MiB).value)
    remaining_mib = total_mib - offset - 1

    root = PartitionModification(
        status=ModificationStatus.CREATE,
        type=PartitionType.Primary,
        start=Size(offset, Unit.MiB, sector),
        length=Size(remaining_mib, Unit.MiB, sector),
        mountpoint=Path("/"),
        fs_type=FilesystemType.EXT4,
    )
    device_mod.add_partition(root)

    return DiskLayoutConfiguration(
        config_type=DiskLayoutType.Default,
        device_modifications=[device_mod],
    )


# ── Kernel helpers ─────────────────────────────────────────────────────────────

def _sanitize_pacman_conf(mountpoint: Path) -> None:
    """
    Remove Include/Server lines from the chroot's pacman.conf that reference
    files not present in the chroot.  This fixes the case where pacstrap copies
    a host-distro pacman.conf (e.g. CachyOS) that includes custom mirrorlist
    files which don't exist in the fresh Arch target.
    """
    conf = mountpoint / "etc" / "pacman.conf"
    if not conf.exists():
        return
    clean_lines = []
    for line in conf.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("Include") and "=" in stripped:
            ref = stripped.split("=", 1)[1].strip()
            target = mountpoint / ref.lstrip("/")
            if not target.exists():
                warn(f"Removing missing Include from pacman.conf: {ref}")
                continue
        clean_lines.append(line)
    conf.write_text("\n".join(clean_lines) + "\n")


def _sanitize_mirrorlist() -> None:
    """
    Remove rsync:// entries from the live system's /etc/pacman.d/mirrorlist.

    pacstrap inherits the host mirrorlist.  rsync mirrors are common in
    CachyOS and some Arch ISO builds but pacman's HTTP stack cannot use them,
    causing 'Protocol "rsync" not supported' errors that abort pacstrap.
    We filter them out in-place, keeping all https:// and http:// mirrors.
    If filtering would leave no mirrors at all we leave the file untouched so
    pacman can surface the real error rather than an empty-mirrorlist error.
    """
    mirrorlist = Path("/etc/pacman.d/mirrorlist")
    if not mirrorlist.exists():
        return

    original = mirrorlist.read_text()
    clean_lines = []
    removed = 0
    for line in original.splitlines():
        stripped = line.strip()
        if stripped.startswith("Server") and "rsync://" in stripped:
            warn(f"Removing rsync mirror from mirrorlist: {stripped}")
            removed += 1
            continue
        clean_lines.append(line)

    if removed == 0:
        return  # nothing to do

    # Sanity check: keep at least one usable Server line
    usable = [l for l in clean_lines if l.strip().startswith("Server")]
    if not usable:
        warn("All mirrors were rsync — leaving mirrorlist untouched to avoid empty list.")
        return

    mirrorlist.write_text("\n".join(clean_lines) + "\n")
    info(f"Removed {removed} rsync mirror(s) from mirrorlist; {len(usable)} mirror(s) remain.")


def _extract_vmlinuz_from_cache(mountpoint: Path) -> None:
    """
    Last-resort fallback: pull vmlinuz-linux directly out of the cached linux
    package with bsdtar — no network or pacman.conf required.
    """
    vmlinuz = mountpoint / "boot" / "vmlinuz-linux"
    if vmlinuz.exists():
        return
    for cache_dir in [
        mountpoint / "var/cache/pacman/pkg",
        Path("/var/cache/pacman/pkg"),
    ]:
        if not cache_dir.exists():
            continue
        candidates = sorted(cache_dir.glob("linux-[0-9]*.pkg.tar.*"), reverse=True)
        for pkg in candidates:
            result = subprocess.run(
                ["bsdtar", "-xOf", str(pkg), "boot/vmlinuz-linux"],
                capture_output=True,
            )
            if result.returncode == 0 and result.stdout:
                vmlinuz.write_bytes(result.stdout)
                info(f"Extracted vmlinuz-linux from {pkg.name}")
                return
    warn("Could not extract vmlinuz-linux from any package cache.")


# ── Main installer ─────────────────────────────────────────────────────────────

def run_install(
    disk,
    disk_config: DiskLayoutConfiguration,
    hostname: str,
    users: list[dict],
    root_pw: str,
    timezone: str,
    kb_layout: str,
    desktop: dict,
    extra_pkgs: list[str],
    swap_mib: int,
) -> None:
    header("Partitioning disk")
    # Deactivate any swap partitions on the target disk that are currently
    # active on the live system.  archinstall's umount_all_existing() uses
    # `umount -R` which cannot handle swap entries and will crash with
    # "umount: [SWAP]: not found" if swap is still on.
    disk_path = str(disk.device_info.path)
    try:
        swap_info = subprocess.run(
            ["swapon", "--show=NAME", "--noheadings"],
            capture_output=True, text=True
        )
        for swap_dev in swap_info.stdout.splitlines():
            if swap_dev.strip().startswith(disk_path):
                subprocess.run(["swapoff", swap_dev.strip()], check=False)
    except Exception:
        pass  # non-fatal — if swapoff fails, perform_filesystem_operations will surface the real error

    # The correct archinstall API for partitioning + formatting is FilesystemHandler,
    # not device_handler.partition().  FilesystemHandler handles both steps together.
    fs_handler = FilesystemHandler(disk_config)
    fs_handler.perform_filesystem_operations()

    base_packages = [
        "base-devel",
        "linux-firmware",
        "nano",
        "vim",
        "networkmanager",
        "sudo",
        "iw",
        "iwd",
        "git",
        "python",
        "python-pip",
        "htop",
        "wget",
        "curl",
        "unzip",
        "zip",
        "man-db",
        "man-pages",
        "bash-completion",
    ]

    all_packages = base_packages + desktop["pkgs"] + extra_pkgs
    # Channel packages are installed AFTER the PineOS repo is added to
    # pacman.conf (post-mkinitcpio).  They must never be in all_packages here
    # because the repo doesn't exist yet when pacstrap/add_additional_packages
    # runs, which would cause pacstrap to abort with "target not found".
    assert not any(p in all_packages for p in PINEOS_CHANNEL["pkg"]), (
        "BUG: channel package leaked into all_packages before repo was configured."
    )

    header("Installing PineOS")

    # ── Pre-pacstrap diagnostics ───────────────────────────────────────────────
    info("Running pre-install diagnostics…")

    # 1. Verify pacman cache exists
    cache_dir = Path("/var/cache/pacman/pkg")
    if not cache_dir.exists():
        warn("pacman cache missing — creating it now.")
        cache_dir.mkdir(parents=True, exist_ok=True)
    else:
        pkg_count = sum(1 for _ in cache_dir.glob("*.pkg.tar.*"))
        info(f"pacman cache: {pkg_count} packages found at {cache_dir}")

    # 2. Verify target is mounted and has usable space
    subprocess.run(["lsblk", "-o", "NAME,SIZE,FSTYPE,MOUNTPOINT"], check=False)
    df = subprocess.run(["df", "-h", str(MOUNTPOINT)], capture_output=True, text=True)
    if df.returncode == 0:
        info(f"Target mount:\n{df.stdout.strip()}")
    else:
        warn(f"Target {MOUNTPOINT} doesn't appear to be mounted yet (expected before pacstrap).")

    # 3. Confirm pacman itself is functional on the live system
    pacman_check = subprocess.run(
        ["pacman", "--version"], capture_output=True, text=True, check=False
    )
    if pacman_check.returncode != 0:
        error("pacman not functional on live system — pacstrap will fail.")
        raise RuntimeError("pacman unavailable on live system.")
    info(f"pacman: {pacman_check.stdout.splitlines()[0].strip()}")

    # 4. Initialize and populate the pacman keyring on the live system.
    #    In VirtualBox (and other VMs) pacstrap's built-in -K keyring init fails
    #    because /dev/random has insufficient entropy and the gnupg keyring
    #    directory ends up non-writable, producing:
    #      "error: keyring is not writable"
    #      "error: required key missing from keyring"
    #    Initializing on the live system first (where entropy is stable) and
    #    then copying the populated keyring into the target avoids this entirely.
    info("Initializing pacman keyring (required for VirtualBox and some live environments)…")
    keyring_ok = True
    for cmd in [
        ["pacman-key", "--init"],
        ["pacman-key", "--populate", "archlinux"],
    ]:
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            warn(f"{' '.join(cmd)} failed (non-fatal, will retry inside chroot if needed).")
            keyring_ok = False

    if keyring_ok:
        # Pre-seed the live keyring into the target so pacstrap doesn't need
        # to re-init it from scratch (avoids the non-writable keyring error).
        live_keyring = Path("/etc/pacman.d/gnupg")
        target_keyring = MOUNTPOINT / "etc/pacman.d/gnupg"
        if live_keyring.exists():
            target_keyring.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["cp", "-a", str(live_keyring), str(target_keyring)],
                check=False,
            )
            info("Live keyring copied to target.")

    # 4. Strip rsync:// mirrors from the live mirrorlist before pacstrap runs.
    #    rsync mirrors are common on CachyOS and some Arch ISOs but pacman's
    #    HTTP client cannot use them, causing pacstrap to fail with:
    #      "Protocol 'rsync' not supported"
    info("Checking mirrorlist for incompatible rsync entries…")
    _sanitize_mirrorlist()

    # 5. Log archinstall install log location for debugging
    info("Detailed pacstrap logs available at: /var/log/archinstall/install.log")

    # archinstall's Installer.__exit__ calls sync_log_to_install_medium() which
    # copies /var/log/archinstall/install.log into the target.  On systems with
    # btrfs subvolume mounts (e.g. /@log mounted at /var/log) both the source
    # and resolved target path can point to the same inode, raising:
    #   OSError: [Errno 22] Source and target are the same path
    # This is a cleanup-only operation — the installation itself is already
    # complete by that point — so we catch and ignore it here.
    try:
        with Installer(
            MOUNTPOINT,
            disk_config,
            kernels=["linux"],
        ) as install:

            install.mount_ordered_layout()

            # ── Base system install ────────────────────────────────────────────
            try:
                header("Installing base system")
                info("Running pacstrap (this may take several minutes)…")
                install.minimal_installation(
                    hostname=hostname,
                    locale_config=LocaleConfiguration(
                        kb_layout=kb_layout,
                        sys_lang="en_US.UTF-8",
                        sys_enc="UTF-8",
                    ),
                )
                info("Base system installed successfully.")

            except Exception as e:
                error(f"pacstrap/minimal_installation failed: {e}")
                error("This often happens in live environments due to mount restrictions.")
                error("Running diagnostics…")
                subprocess.run(["ls", "-la", str(MOUNTPOINT)], check=False)
                subprocess.run(["df", "-h"], check=False)
                subprocess.run(["mount"], check=False)
                raise

            install.add_additional_packages(all_packages)

            # Timezone
            install.set_timezone(timezone)

            # ── Kernel & initramfs ─────────────────────────────────────────────
            # On constrained live environments (Chromebooks, testing from a
            # non-Arch host like CachyOS) pacstrap fails the linux post-install
            # hook with "execv: No such file or directory", which leaves
            # /boot/vmlinuz-linux unwritten and /etc/mkinitcpio.d/linux.preset
            # missing.  We fix this in two stages:
            #
            # Stage 1 — sanitize the chroot's pacman.conf.
            #   pacstrap copies the HOST's pacman.conf into the chroot.  If the
            #   host is CachyOS (or any distro with custom repos), the chroot
            #   will reference mirrorlist files that don't exist there, causing
            #   every subsequent `pacman` call inside the chroot to abort.
            #   We strip out any Include/Server lines that point to missing files.
            #
            # Stage 2 — reinstall linux inside the chroot.
            #   With a working pacman.conf the chroot's pacman can reinstall the
            #   linux package.  Inside the fully-set-up chroot, hooks execute
            #   properly, writing vmlinuz-linux and linux.preset to the right
            #   locations.  If pacman still can't run for any reason we fall back
            #   to extracting vmlinuz directly from the pacman package cache with
            #   bsdtar (no network, no pacman.conf needed).

            info("Sanitizing chroot pacman.conf…")
            _sanitize_pacman_conf(MOUNTPOINT)

            info("Ensuring kernel image is present (reinstalling linux in chroot)…")
            linux_ok = subprocess.run(
                ["arch-chroot", str(MOUNTPOINT),
                 "pacman", "-S", "--noconfirm", "--overwrite=*", "linux"],
                check=False,
            ).returncode == 0

            if not linux_ok:
                warn("pacman reinstall failed — extracting vmlinuz from package cache…")
                _extract_vmlinuz_from_cache(MOUNTPOINT)

            # Write preset if still absent (hooks may be disabled)
            info("Regenerating initramfs…")
            preset_path = MOUNTPOINT / "etc" / "mkinitcpio.d" / "linux.preset"
            preset_path.parent.mkdir(parents=True, exist_ok=True)
            if not preset_path.exists():
                warn("linux.preset missing — writing standard preset.")
                preset_path.write_text(
                    "# mkinitcpio preset for the 'linux' package (written by PineOS installer)\n"
                    "ALL_config='/etc/mkinitcpio.conf'\n"
                    "ALL_kver='/boot/vmlinuz-linux'\n"
                    "PRESETS=('default' 'fallback')\n"
                    "default_image='/boot/initramfs-linux.img'\n"
                    "fallback_image='/boot/initramfs-linux-fallback.img'\n"
                    "fallback_options='-S autodetect'\n"
                )
            install.arch_chroot("mkinitcpio -P")

            # ── PineOS repositories & channel packages ─────────────────────────
            # Add the official PineOS repo to the target's pacman.conf, then
            # install the channel packages.
            info(f"Adding PineOS repositories ({PINEOS_CHANNEL['label'].strip()})…")
            target_pacman_conf = MOUNTPOINT / "etc" / "pacman.conf"
            with open(target_pacman_conf, "a") as f:
                f.write(PINEOS_CHANNEL["repo_block"])

            # Sync the new repo and install channel packages
            install.arch_chroot("pacman -Sy --noconfirm")
            if PINEOS_CHANNEL["pkg"]:
                pkgs_str = " ".join(PINEOS_CHANNEL["pkg"])
                info(f"Installing PineOS channel packages: {pkgs_str}")
                result = subprocess.run(
                    ["arch-chroot", str(MOUNTPOINT),
                     "pacman", "-S", "--noconfirm", "--needed"] + PINEOS_CHANNEL["pkg"],
                    check=False,
                )
                if result.returncode != 0:
                    warn("Some PineOS channel packages could not be installed "
                         "(repo may not be live yet). Continuing without them.")

            # Bootloader — install GRUB directly via arch_chroot rather than using
            # install.add_bootloader().  archinstall's add_bootloader() internally
            # runs the same grub-install command but its EFI-partition auto-detection
            # consistently fails on single-partition layouts (/boot = ESP), logging
            # "Could not detect efi partition" regardless of mountpoint used.
            # Running grub-install ourselves gives full control and no detection step.
            install.add_additional_packages(["grub", "efibootmgr", "os-prober"])

            efi_dir = "/boot"   # EFI partition is mounted at /boot
            install.arch_chroot(
                f"grub-install --target=x86_64-efi "
                f"--efi-directory={efi_dir} "
                f"--bootloader-id=PineOS "
                f"--recheck"
            )
            # Enable os-prober so dual-boot entries are picked up
            install.arch_chroot(
                "sed -i 's/#GRUB_DISABLE_OS_PROBER=false/GRUB_DISABLE_OS_PROBER=false/' "
                "/etc/default/grub"
            )
            install.arch_chroot("grub-mkconfig -o /boot/grub/grub.cfg")

            # ── Users ─────────────────────────────────────────────────────────
            for u in users:
                user_obj = User(u["username"], Password(plaintext=u["password"]), sudo=u["sudo"])
                install.create_users(user_obj)

            # Set root password safely via stdin (avoids shell injection)
            subprocess.run(
                ["arch-chroot", str(MOUNTPOINT), "chpasswd"],
                input=f"root:{root_pw}\n".encode(),
                check=True,
            )

            # Services
            install.enable_service("NetworkManager")
            install.enable_service("iwd")
            for svc in desktop["services"]:
                install.enable_service(svc)

            # Enable fstrim for SSDs
            install.enable_service("fstrim.timer")

            # sudoers — wheel group
            sudoers = MOUNTPOINT / "etc" / "sudoers.d" / "10-wheel"
            sudoers.parent.mkdir(parents=True, exist_ok=True)
            sudoers.write_text("%wheel ALL=(ALL:ALL) ALL\n")
            sudoers.chmod(0o440)

            # MOTD
            (MOUNTPOINT / "etc" / "motd").write_text(MOTD_TEXT)

            # PineOS release file
            (MOUNTPOINT / "etc" / "pineos-release").write_text(
                f"NAME=PineOS\n"
                f"VERSION={VERSION}\n"
                f"ID=pineos\n"
                f"ID_LIKE=arch\n"
                f"PRETTY_NAME=\"PineOS {VERSION}\"\n"
                f"HOME_URL=https://github.com/The-Pine-Project\n"
            )

            # FIX #6: os-release symlink — always unlink first so the base
            # package's existing /etc/os-release doesn't block creation.
            os_release = MOUNTPOINT / "etc" / "os-release"
            if os_release.exists() or os_release.is_symlink():
                os_release.unlink()
            os_release.symlink_to("pineos-release")

            header("Post-install finalisation")
            info("Syncing filesystems…")

    except OSError as e:
        # archinstall's __exit__ calls sync_log_to_install_medium() which copies
        # the install log into the target system.  On machines with btrfs
        # subvolume mounts (/@log → /var/log) the source and resolved target
        # are the same inode, raising OSError [Errno 22].  The installation is
        # already complete at this point, so we catch and note it, then continue.
        if e.errno == 22 and "same path" in str(e):
            warn("Ignoring benign log-copy error from archinstall cleanup.")
        else:
            raise

    info("Unmounting target…")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    os.system("clear")
    print(f"\n{GREEN}{BOLD}")
    print("  ██████╗ ██╗███╗   ██╗███████╗ ██████╗ ███████╗")
    print("  ██╔══██╗██║████╗  ██║██╔════╝██╔═══██╗██╔════╝")
    print("  ██████╔╝██║██╔██╗ ██║█████╗  ██║   ██║███████╗")
    print("  ██╔═══╝ ██║██║╚██╗██║██╔══╝  ██║   ██║╚════██║")
    print("  ██║     ██║██║ ╚████║███████╗╚██████╔╝███████║")
    print("  ╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝ ╚═════╝ ╚══════╝")
    print(f"{RESET}")
    print(f"  {BOLD}PineOS Installer{RESET}  —  version {VERSION}")
    print(f"  https://github.com/The-Pine-Project\n")

    # Check for a newer version of the installer before prompting
    check_for_updates()

    # Gather all configuration interactively
    disk                    = pick_disk()
    swap_mib                = get_swap_size()
    hostname, users, root_pw = get_credentials()
    timezone, kb_layout     = get_locale_settings()
    desktop                 = get_desktop()
    extra_pkgs              = get_extra_packages()

    review_and_confirm(
        disk, hostname, users, timezone, kb_layout,
        swap_mib, desktop, extra_pkgs,
    )

    # Build disk layout
    disk_config = build_disk_layout(disk, swap_mib)

    # Run installation
    run_install(
        disk, disk_config,
        hostname, users, root_pw,
        timezone, kb_layout,
        desktop, extra_pkgs,
        swap_mib,
    )

    header("Installation complete")
    print()
    info(f"PineOS {VERSION} has been installed successfully.")
    info("Remove the installation media and reboot.")
    print()
    if confirm("Reboot now?", default=True):
        subprocess.run(["reboot"])


if __name__ == "__main__":
    # FIX #8: Warn about unrecognised CLI arguments rather than silently
    # dropping them (the shell launcher passes "$@" through).
    if len(sys.argv) > 1:
        warn_args = ", ".join(sys.argv[1:])
        print(f"  {YELLOW}⚠{RESET}  Ignoring unrecognised arguments: {warn_args}")

    try:
        main()
    except KeyboardInterrupt:
        print()
        error("Installation interrupted by user.")
        sys.exit(1)
    except Exception as exc:
        print()
        error(f"Unexpected error: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

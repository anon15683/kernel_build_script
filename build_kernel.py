#!/usr/bin/env python3

import os
import sys
import subprocess
import json
import shutil
import argparse
import datetime
import re
import stat
import tempfile
import math
import glob
from pathlib import Path
from textwrap import dedent
from typing import Optional

# ─── Build identity ───────────────────────────────────────────────────────────

ROOT_DIR             = Path(__file__).resolve().parent
ARCH                 = "arm64"
TARGET_SOC           = "s5e8845"
VARIANT              = "user"
TARGET_DEVICE        = "a55x"
CROSS_COMPILE_PREFIX = "aarch64-linux-gnu-"
KERNEL_DEFCONFIG     = "essi_defconfig"

# ─── Directory layout ─────────────────────────────────────────────────────────

PREBUILTS_BASE_DIR = ROOT_DIR.parent / "prebuilts"
BUILD_LOG_FILE     = ROOT_DIR / "kernel_build.log"

VENDOR_RAMDISK_DLKM_EARLY_MODULES_FILE = ROOT_DIR / "modules.early.load"
VENDOR_RAMDISK_DLKM_MODULES_FILE       = ROOT_DIR / "modules.load"
VENDOR_DLKM_MODULES_FILE               = ROOT_DIR / "modules.load.vendor_dlkm"

# ─── Global paths (populated by setup_environment) ────────────────────────────

OUT_DIR              = None
DIST_DIR             = None
MODULES_STAGING_DIR  = None
TOOLCHAIN_PATH       = None
KERNELBUILD_TOOLS_PATH = None
GAS_PATH             = None
MKBOOT_PATH          = None
ANYKERNEL_PATH       = None
KERNEL_SOURCE_DIR    = None
KSU_NEXT_PATH        = None
SUKISU_PATH          = None
SUSFS_PATH           = None

_prebuilts_json  = json.load(open(ROOT_DIR / "prebuilts.json"))
PREBUILTS_CONFIG = _prebuilts_json["prebuilts"]
URLS_CONFIG      = _prebuilts_json["urls"]

# ─── DroidSpaces constants ────────────────────────────────────────────────────

DROIDSPACES_BASE_URL = URLS_CONFIG["DroidSpaces"]["base_url"]

DROIDSPACES_PATCHES = {
    1: "001.GKI-below-6.12-fix_sysvipc_kabi_6_7_8.patch",
    2: "001.GKI-below-6.12-fix_sysvipc_kabi_3_4_5.patch",
    3: "001.GKI-below-6.12-fix_sysvipc_kabi_1_2_3.patch",
}

DROIDSPACES_COMBINATIONS = {
    1:   [1],
    2:   [2],
    3:   [3],
    12:  [1, 2],
    13:  [1, 3],
    23:  [2, 3],
    123: [1, 2, 3],
}

# Kernel configs required/recommended for full DroidSpaces support on GKI.
# Copyright (C) 2026 ravindu644 <droidcasts@protonmail.com>
DROIDSPACES_KERNEL_CONFIGS = [
    # IPC
    "CONFIG_SYSVIPC",
    "CONFIG_POSIX_MQUEUE",
    # Namespaces
    "CONFIG_IPC_NS",
    "CONFIG_PID_NS",
    # HW access
    "CONFIG_DEVTMPFS",
    # Networking – enhanced NAT
    "CONFIG_NETFILTER_XT_MATCH_ADDRTYPE",
    # UFW support (optional but recommended)
    "CONFIG_NETFILTER_XT_TARGET_REJECT",
    "CONFIG_NETFILTER_XT_TARGET_LOG",
    "CONFIG_NETFILTER_XT_MATCH_RECENT",
    # Fail2ban support (optional but recommended)
    "CONFIG_IP_SET",
    "CONFIG_IP_SET_HASH_IP",
    "CONFIG_IP_SET_HASH_NET",
    "CONFIG_NETFILTER_XT_SET",
    # tmpfs xattr/POSIX-ACL – NixOS support (optional but recommended)
    "CONFIG_TMPFS_POSIX_ACL",
    "CONFIG_TMPFS_XATTR",
]

# ─── Logging ──────────────────────────────────────────────────────────────────

def log_message(message: str):
    """Log to console and append to the build log file."""
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{timestamp} - {message}"
    print(line)
    try:
        BUILD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BUILD_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Logging failed: {e}")

# ─── Shell helpers ────────────────────────────────────────────────────────────

def run_cmd(command: str,
            cwd: Optional[Path] = None,
            extra_env: Optional[dict[str, str]] = None,
            fatal_on_error: bool = True) -> Optional[str]:
    """
    Run a shell command, log it, and return stdout.

    Args:
        command:        Shell command to run.
        cwd:            Working directory (optional).
        extra_env:      Extra environment variables (optional).
        fatal_on_error: Call sys.exit(1) on non-zero exit if True.

    Returns:
        stdout string, or None on failure when fatal_on_error is False.
    """
    log_message(
        f"Running: '{command}' in '{cwd.resolve()}'"
        if cwd else f"Running: '{command}'"
    )

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        log_message("Command succeeded")
        return result.stdout
    except subprocess.CalledProcessError as e:
        log_message(f"[ERROR] Command failed (exit {e.returncode}): '{command}'")
        if e.stdout:
            log_message(f"stdout:\n{e.stdout.strip()}")
        if e.stderr:
            log_message(f"stderr:\n{e.stderr.strip()}")
        if fatal_on_error:
            sys.exit(1)
        return None
    except Exception as e:
        log_message(f"[CRITICAL] Unexpected exception: {e}")
        sys.exit(1)


def _make_downloader():
    """Return a (url, out) -> None callable using wget or curl, or None."""
    if shutil.which("wget"):
        return lambda url, out: run_cmd(f"wget -q -O '{out}' '{url}'", fatal_on_error=True)
    if shutil.which("curl"):
        return lambda url, out: run_cmd(f"curl -s -L -o '{out}' '{url}'", fatal_on_error=True)
    return None

# ─── Environment / prebuilts ──────────────────────────────────────────────────

def get_version_env() -> dict[str, str]:
    """
    Return BRANCH and KMI_GENERATION from kernel build-config files.
    Exits if any required file is missing.
    """
    config_files = {
        "BRANCH":          KERNEL_SOURCE_DIR / "build.config.constants",
        "KMI_GENERATION":  KERNEL_SOURCE_DIR / "build.config.common",
    }
    result = {}
    for key, path in config_files.items():
        try:
            for line in path.read_text().splitlines():
                if line.strip().startswith(f"{key}="):
                    result[key] = line.split("=", 1)[1].strip().strip('"\'')
                    break
        except FileNotFoundError:
            log_message(f"ERROR: Missing config file: {path}")
            sys.exit(1)
    return result


def validate_prebuilts():
    """Verify all required prebuilt paths exist; set OUT/DIST/MODULES_STAGING dirs."""
    log_message("Checking required prebuilts...")

    global OUT_DIR, DIST_DIR, MODULES_STAGING_DIR

    required = {
        "Toolchain":         TOOLCHAIN_PATH,
        "Kernel Build Tools": KERNELBUILD_TOOLS_PATH,
        "GAS":               GAS_PATH,
        "Mkbootimg Tool":    MKBOOT_PATH,
        "Anykernel3":        ANYKERNEL_PATH,
        "Kernel Source":     KERNEL_SOURCE_DIR,
    }
    for name, path in required.items():
        if not path or not path.is_dir():
            log_message(f"[ERROR] Missing or invalid: {name} -> '{path}'")
            sys.exit(1)

    OUT_DIR             = KERNEL_SOURCE_DIR / "out"
    DIST_DIR            = KERNEL_SOURCE_DIR.parent / "out" / "dist"
    MODULES_STAGING_DIR = OUT_DIR / "modules_install"

    log_message("All prebuilts verified")


def unpack_tarball(archive_path: Path, dest_dir: Path):
    """
    Extract a .tar.gz archive to dest_dir.
    If it contains a single top-level directory, that directory is flattened.
    """
    log_message(f"Extracting '{archive_path}' to '{dest_dir}'...")

    temp_dir = archive_path.parent / f"temp_extract_{os.getpid()}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)

    run_cmd(f"tar -xzf {archive_path} -C {temp_dir}", fatal_on_error=True)

    contents = list(temp_dir.iterdir())
    dest_dir.mkdir(parents=True, exist_ok=True)

    source = contents[0] if len(contents) == 1 and contents[0].is_dir() else temp_dir
    if source != temp_dir:
        log_message(f"Flattening archive from '{source}'...")
    for item in source.iterdir():
        shutil.move(str(item), str(dest_dir / item.name))

    shutil.rmtree(temp_dir, ignore_errors=True)
    log_message(f"Extraction complete: '{archive_path.name}'")


def get_prebuilt(name: str, config: dict, target_dir: Path):
    """
    Fetch a prebuilt (tarball or git) if absent; update git repos when present.
    Skips update when config['skip_update'] is True.
    """
    log_message(f"Checking prebuilt '{name}' at '{target_dir}' ...")

    marker_file = target_dir / ".prebuilt_ready"
    if target_dir.exists():
        if not marker_file.exists():
            log_message(f"'{name}' exists but marker absent")
            if config["download_type"] == "git":
                log_message("Marking as ready (skipping clone to avoid git error)")
                marker_file.touch()
                return
        elif config.get("skip_update", False):
            log_message(f"'{name}' exists, skipping update (--skip-prebuilt-update)")
            return

        if config["download_type"] == "git" and (target_dir / ".git").is_dir():
            log_message(f"Updating git repo for '{name}' ...")
            run_cmd("git pull --recurse-submodules", cwd=target_dir, fatal_on_error=False)

        marker_file.touch()
        return

    log_message(f"'{name}' not found, fetching...")
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    download_type = config["download_type"]
    if download_type == "download_url":
        archive = ROOT_DIR / f"temp_{name.lower().replace(' ', '_')}.tar.gz"
        url = config["download_url"]
        log_message(f"Downloading '{name}' from: {url}")

        downloader = _make_downloader()
        if downloader is None:
            log_message("ERROR: wget or curl not found")
            sys.exit(1)
        downloader(url, archive)

        log_message("Download complete. Extracting...")
        unpack_tarball(archive, target_dir)
        os.remove(archive)
        log_message(f"Extraction complete: {target_dir}")
        marker_file.touch()

    elif download_type == "git":
        repo   = config["repo_url"]
        branch = config["branch"]
        depth  = config.get("depth")
        depth_arg = f"--depth {depth} --shallow-submodules" if depth else ""
        log_message(f"Cloning '{repo}' (branch: {branch})")
        run_cmd(
            f"git clone --recurse-submodules {depth_arg} --branch {branch} {repo} {target_dir}",
            fatal_on_error=True,
        )
        log_message(f"Cloned to: {target_dir}")
        marker_file.touch()

    else:
        log_message(f"ERROR: Unknown download_type '{download_type}'")
        sys.exit(1)


def setup_environment(skip_prebuilt_update: bool = False):
    """
    Download/update all prebuilts and populate global path variables.
    Also extends PATH with toolchain and tool directories.
    """
    log_message("Initializing environment...")

    global TOOLCHAIN_PATH, GAS_PATH, KERNELBUILD_TOOLS_PATH
    global MKBOOT_PATH, ANYKERNEL_PATH, KERNEL_SOURCE_DIR
    global KSU_NEXT_PATH, SUKISU_PATH, SUSFS_PATH

    os.environ["ARCH"]         = ARCH
    os.environ["CROSS_COMPILE"] = CROSS_COMPILE_PREFIX
    os.environ["TARGET_SOC"]   = TARGET_SOC
    log_message(
        f"Set env: ARCH={ARCH}, CROSS_COMPILE={CROSS_COMPILE_PREFIX}, TARGET_SOC={TARGET_SOC}"
    )

    for name, config in PREBUILTS_CONFIG.items():
        target = (
            ROOT_DIR.parent / config["target_dir_name"]
            if name == "Kernel_Source"
            else PREBUILTS_BASE_DIR / config["target_dir_name"]
        )
        config["skip_update"] = skip_prebuilt_update
        get_prebuilt(name, config, target)
        if name == "Kernel_Source":
            KERNEL_SOURCE_DIR = target

    def _pb(key, *suffixes):
        base = PREBUILTS_BASE_DIR / PREBUILTS_CONFIG[key]["target_dir_name"]
        for s in suffixes:
            base = base / s
        return base

    TOOLCHAIN_PATH         = _pb("Toolchain", PREBUILTS_CONFIG["Toolchain"]["bin_path_suffix"])
    KERNELBUILD_TOOLS_PATH = _pb("Kernel_Build_Tools", PREBUILTS_CONFIG["Kernel_Build_Tools"]["bin_path_suffix"])
    GAS_PATH               = _pb("GAS")
    MKBOOT_PATH            = _pb("Mkbootimg_Tool")
    ANYKERNEL_PATH         = _pb("Anykernel3")
    KERNEL_SOURCE_DIR      = ROOT_DIR.parent / PREBUILTS_CONFIG["Kernel_Source"]["target_dir_name"]
    KSU_NEXT_PATH          = KERNEL_SOURCE_DIR / "KernelSU-Next"
    SUSFS_PATH             = _pb("SUSFS")
    SUKISU_PATH            = KERNEL_SOURCE_DIR / "KernelSU"

    log_message("Updating PATH...")
    extra_paths = [
        str(p) for p in [
            TOOLCHAIN_PATH, KERNELBUILD_TOOLS_PATH, GAS_PATH,
            MKBOOT_PATH, ANYKERNEL_PATH, KERNEL_SOURCE_DIR,
            KSU_NEXT_PATH, SUSFS_PATH,
        ] if p
    ]
    current = os.environ["PATH"].split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        [p for p in extra_paths if p not in current] + current
    )
    log_message(f"New PATH: {os.environ['PATH']}")
    log_message("Environment setup complete")

# ─── Build steps ──────────────────────────────────────────────────────────────

def clean_build_artifacts():
    """Run make clean/mrproper, reset git-tracked changes, and remove OUT_DIR."""
    log_message("Cleaning kernel build artifacts...")

    run_cmd("make clean",    cwd=KERNEL_SOURCE_DIR, fatal_on_error=False)
    run_cmd("make mrproper", cwd=KERNEL_SOURCE_DIR, fatal_on_error=False)

    run_cmd("git checkout -- .", cwd=KERNEL_SOURCE_DIR, fatal_on_error=True)
    log_message("Reset modified source files")

    run_cmd("git clean -fd", cwd=KERNEL_SOURCE_DIR, fatal_on_error=True)
    log_message("Removed untracked source files")

    if OUT_DIR.exists():
        log_message(f"Removing output directory: '{OUT_DIR}'")
        shutil.rmtree(OUT_DIR, ignore_errors=True)

    log_message("Clean complete")


def build_kernel(jobs: int,
                 extra_env: Optional[dict[str, str]] = None,
                 install_modules: bool = False) -> None:
    """
    Build the Android kernel image.

    Args:
        jobs:            Number of parallel make jobs.
        extra_env:       Extra env vars (e.g. BRANCH, KMI_GENERATION).
        install_modules: Install .ko files to the staging directory when True.
    """
    log_message(f"Starting kernel build with {jobs} parallel jobs...")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    MODULES_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    cc = "ccache clang" if shutil.which("ccache") else "clang"
    make_args = (
        f"LLVM=1 LLVM_IAS=1 ARCH={ARCH} O={OUT_DIR} "
        f"CROSS_COMPILE={CROSS_COMPILE_PREFIX} CC='{cc}'"
    )

    log_message(f"Using defconfig: '{KERNEL_DEFCONFIG}'")
    run_cmd(f"make {make_args} {KERNEL_DEFCONFIG}", cwd=KERNEL_SOURCE_DIR, fatal_on_error=True)

    log_message("Compiling kernel Image...")
    run_cmd(f"make -j{jobs} {make_args}", cwd=KERNEL_SOURCE_DIR,
            extra_env=extra_env, fatal_on_error=True)

    if install_modules:
        log_message(f"Installing modules to: {MODULES_STAGING_DIR}...")
        run_cmd(
            f"make -j{jobs} {make_args} "
            f"INSTALL_MOD_STRIP='--strip-debug --keep-section=.ARM.attributes' "
            f"INSTALL_MOD_PATH={MODULES_STAGING_DIR} modules_install",
            cwd=KERNEL_SOURCE_DIR,
        )

    image_src = OUT_DIR / "arch" / ARCH / "boot" / "Image"
    image_dst = DIST_DIR / "Image"
    try:
        shutil.copyfile(image_src, image_dst)
    except Exception as e:
        log_message(f"ERROR: Failed to copy kernel Image: {e}")
        sys.exit(1)

    log_message("Kernel build completed")


def build_dtbo_images():
    """
    Generate dtbo.img (via mkdtimg) and dtb.img (concatenation) from compiled
    device-tree files, preserving the build order from the source Makefile.
    """
    arch_dts = OUT_DIR / "arch" / ARCH / "boot" / "dts"
    dtbo_dir = arch_dts / "samsung" / TARGET_DEVICE
    dtb_dir  = arch_dts / "exynos"

    makefile_path = (
        KERNEL_SOURCE_DIR / "arch" / ARCH / "boot" / "dts"
        / "samsung" / TARGET_DEVICE / "Makefile"
    )
    ordered_dtbos = []
    if makefile_path.exists():
        with open(makefile_path) as f:
            for line in f:
                m = re.search(r'dtb-y\s*\+=\s*(\S+\.dtbo)', line)
                if m:
                    p = dtbo_dir / m.group(1)
                    if p.exists():
                        ordered_dtbos.append(p)

    if ordered_dtbos:
        dtbo_files = ordered_dtbos
    else:
        log_message("WARNING: Could not determine dtbo order from Makefile; falling back to alphabetical sort")
        dtbo_files = sorted(dtbo_dir.glob("*.dtbo"))

    dtb_files = sorted(dtb_dir.glob("*.dtb"))

    if not dtbo_files:
        log_message(f"ERROR: No *.dtbo files found in {dtbo_dir}")
        sys.exit(1)
    if not dtb_files:
        log_message(f"ERROR: No *.dtb files found in {dtb_dir}")
        sys.exit(1)

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    custom_flags = (
        "--custom0=/:dtbo-hw_rev "
        "--custom1=/:dtbo-hw_rev_end "
        "--custom2=/:edtbo-rev"
    )
    run_cmd(
        f"{KERNELBUILD_TOOLS_PATH / 'mkdtimg'} create {DIST_DIR / 'dtbo.img'} "
        f"{custom_flags} " + " ".join(str(f) for f in dtbo_files),
        fatal_on_error=True,
    )

    with open(DIST_DIR / "dtb.img", "wb") as out_f:
        for dtb in dtb_files:
            out_f.write(dtb.read_bytes())

    log_message("Built dtbo.img and dtb.img")


def build_boot_image():
    """Build boot.img (header v4) from the compiled kernel Image."""
    kernel_image = OUT_DIR / "arch" / ARCH / "boot" / "Image"
    bootimg_out  = DIST_DIR / "boot.img"

    if not kernel_image.is_file():
        log_message(f"ERROR: Kernel image not found: {kernel_image}")
        sys.exit(1)

    run_cmd(
        f"{MKBOOT_PATH / 'mkbootimg.py'} "
        f"--kernel {kernel_image} "
        f"--output {bootimg_out} "
        f"--pagesize 4096 "
        f"--header_version 4",
        fatal_on_error=True,
    )

    if not bootimg_out.exists():
        log_message("ERROR: boot.img was not created")
        sys.exit(1)

    log_message(f"boot.img created at {bootimg_out}")


def create_flash_zip():
    """Package the built kernel Image into a flashable AnyKernel3 ZIP."""
    image_path = DIST_DIR / "Image"
    if not image_path.exists():
        log_message(f"ERROR: Kernel Image not found: {image_path}")
        sys.exit(1)

    tmp_dir     = Path(tempfile.mkdtemp(prefix="anykernel3_"))
    staging_dir = tmp_dir / "AnyKernel3"
    try:
        shutil.copytree(ANYKERNEL_PATH, staging_dir, dirs_exist_ok=True)
        shutil.copy(ROOT_DIR / "src" / "anykernel.sh", staging_dir / "anykernel.sh")
        shutil.copy(image_path, staging_dir / "Image")
        log_message(f"Copied Image to temp AnyKernel3 folder: {staging_dir / 'Image'}")

        timestamp  = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        output_zip = DIST_DIR / f"{TARGET_DEVICE}-{VARIANT}-{timestamp}.zip"
        run_cmd(f"cd {staging_dir} && zip -r9 {output_zip} * -x .git/*", fatal_on_error=True)
        log_message(f"Created flashable ZIP: {output_zip}")
    except Exception as e:
        log_message(f"ERROR during flash ZIP creation: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ─── Module helpers ───────────────────────────────────────────────────────────

def read_modules_file(file_path: Path) -> list[str]:
    """
    Read a module-list file (one name per line).
    Blank lines and lines starting with '#' are ignored.
    Exits if the file does not exist.
    """
    if not file_path.is_file():
        log_message(f"ERROR: Module list file not found: {file_path}")
        sys.exit(1)
    with open(file_path) as f:
        return [l.strip() for l in f if l.strip() and not l.startswith('#')]


def _resolve_kernel_version(base_modules_dir: Path) -> str:
    """Return the kernel version directory name under base_modules_dir, or exit."""
    dirs = list(base_modules_dir.glob("*-*"))
    if not dirs:
        log_message("ERROR: No kernel version directory found")
        sys.exit(1)
    version = dirs[0].name
    log_message(f"Kernel version: {version}")
    return version


def _rewrite_modules_dep(dep_file: Path, mount_prefix: str):
    """Rewrite modules.dep so every path is prefixed with mount_prefix/lib/modules/."""
    if not dep_file.exists():
        log_message("ERROR: modules.dep not found")
        sys.exit(1)
    new_lines = []
    with open(dep_file) as f:
        for line in f:
            parts = line.strip().split(":", 1)
            main = f"{mount_prefix}/lib/modules/{parts[0].strip()}"
            deps = (
                " ".join(f"{mount_prefix}/lib/modules/{d.strip()}"
                         for d in parts[1].split())
                if len(parts) == 2 else ""
            )
            new_lines.append(f"{main}: {deps}".rstrip())
    with open(dep_file, "w") as f:
        f.write("\n".join(new_lines))


def _copy_builtin_files(src_dir: Path, dst_dir: Path):
    """Copy modules.builtin* files from src_dir to dst_dir (warn if absent)."""
    for name in ["modules.builtin", "modules.builtin.modinfo",
                 "modules.builtin.alias.bin", "modules.builtin.bin"]:
        src = src_dir / name
        if src.exists():
            shutil.copy(src, dst_dir / name)
        else:
            log_message(f"WARNING: {name} not found in {src_dir}")


def _write_modules_load_and_order(dst_dir: Path, module_names: list[str]):
    """Write modules.load and modules.order listing all *.ko entries."""
    for filename in ["modules.load", "modules.order"]:
        with open(dst_dir / filename, "w") as f:
            for name in module_names:
                if name.endswith(".ko"):
                    f.write(name + "\n")


def _require_tools(tools_path: Path, *tool_names: str) -> dict[str, Path]:
    """
    Verify each tool exists under tools_path, mark it executable, and return
    a name -> Path mapping. Exits on the first missing tool.
    """
    result = {}
    for name in tool_names:
        tool = tools_path / name
        if not tool.is_file():
            log_message(f"ERROR: {name} not found: {tool}")
            sys.exit(1)
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        result[name] = tool
    return result

# ─── Image builders ───────────────────────────────────────────────────────────

def mk_vendor_rd_dlkm(mount_prefix: str,
                      module_early_list_file: Path,
                      module_list_file: Path):
    """
    Build vendor_ramdisk_dlkm.cpio.lz4 from the staged kernel modules.

    Args:
        mount_prefix:           Ramdisk mount point (e.g. "vendor_ramdisk_dlkm").
        module_early_list_file: Path to the early-load module list.
        module_list_file:       Path to the normal module list.
    """
    early_modules  = read_modules_file(module_early_list_file)
    normal_modules = read_modules_file(module_list_file)
    vendor_modules = early_modules + normal_modules

    if not vendor_modules:
        log_message("ERROR: Module list is empty")
        sys.exit(1)

    base_modules_dir = Path(MODULES_STAGING_DIR) / "lib" / "modules"
    kernel_version   = _resolve_kernel_version(base_modules_dir)

    staging_dir      = Path(tempfile.mkdtemp(prefix="vendor_ramdisk_dlkm_staging_"))
    flat_dir         = staging_dir / "lib" / "modules" / kernel_version
    flat_dir.mkdir(parents=True, exist_ok=True)

    final_output     = Path(DIST_DIR) / "vendor_ramdisk_dlkm.cpio.lz4"
    final_output.parent.mkdir(parents=True, exist_ok=True)
    output_cpio      = staging_dir.parent / "vendor_ramdisk_dlkm.cpio"

    for name in vendor_modules:
        found = list(base_modules_dir.rglob(name))
        if not found:
            log_message(f"ERROR: Module not found: {name}")
            sys.exit(1)
        shutil.copy(found[0], flat_dir / name)

    tools = _require_tools(Path(KERNELBUILD_TOOLS_PATH), "mkbootfs", "lz4", "depmod")

    run_cmd(f"{tools['depmod']} -b {staging_dir} {kernel_version}", fatal_on_error=True)

    # Flatten lib/modules/<version>/ → lib/modules/
    flat_mod_root = staging_dir / "lib" / "modules"
    for item in flat_dir.iterdir():
        shutil.move(str(item), flat_mod_root / item.name)
    shutil.rmtree(flat_dir)

    _rewrite_modules_dep(flat_mod_root / "modules.dep", mount_prefix)
    _copy_builtin_files(base_modules_dir / kernel_version, flat_mod_root)
    _write_modules_load_and_order(flat_mod_root, vendor_modules)

    try:
        with open(output_cpio, "wb") as out:
            subprocess.run([str(tools["mkbootfs"]), str(staging_dir)],
                           stdout=out, check=True)
        run_cmd(f"{tools['lz4']} -9 -f -l {output_cpio} {final_output}", fatal_on_error=True)
    except Exception as e:
        log_message(f"ERROR during CPIO creation or compression: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        output_cpio.unlink(missing_ok=True)


def get_system_dlkm_list() -> list[str]:
    """
    Parse modules.bzl for _COMMON_GKI_MODULES_LIST and _ARM64_GKI_MODULES_LIST.

    Returns:
        Sorted, deduplicated list of .ko filenames.
    """
    bzl_path = KERNEL_SOURCE_DIR / "modules.bzl"
    markers  = {"_COMMON_GKI_MODULES_LIST", "_ARM64_GKI_MODULES_LIST"}
    modules  = set()

    try:
        with open(bzl_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        log_message(f"ERROR: modules.bzl not found: {bzl_path}")
        sys.exit(1)

    capture = False
    for line in lines:
        if any(m in line for m in markers):
            capture = True
            continue
        if capture:
            match = re.search(r'^\s*"([^"]+\.ko)"', line)
            if match:
                modules.add(Path(match.group(1)).name)
            elif "]" in line:
                capture = False

    return sorted(modules)


def build_dlkm_image(image_name: str,
                     modules_list_file: Optional[Path],
                     mount_prefix: str,
                     sign_modules: bool = False):
    """
    Build a DLKM image in EROFS format.

    Args:
        image_name:        Output image base name (e.g. "system_dlkm").
        modules_list_file: Module list file (ignored for system_dlkm).
        mount_prefix:      Mount point inside the image (e.g. "/system_dlkm").
        sign_modules:      Sign each .ko with sign-file when True.
    """
    if image_name == "system_dlkm":
        log_message("Reading system_dlkm modules from modules.bzl...")
        modules = get_system_dlkm_list()
    else:
        modules = read_modules_file(modules_list_file)

    if not modules:
        log_message(f"ERROR: No modules found for {image_name}")
        sys.exit(1)

    base_modules_dir = Path(MODULES_STAGING_DIR) / "lib" / "modules"
    kernel_version   = _resolve_kernel_version(base_modules_dir)

    final_img = Path(DIST_DIR) / f"{image_name}.img"
    final_img.parent.mkdir(parents=True, exist_ok=True)

    sign_tool = OUT_DIR / "scripts" / "sign-file"
    key_pem   = OUT_DIR / "certs" / "signing_key.pem"
    key_x509  = OUT_DIR / "certs" / "signing_key.x509"

    staging_dir = Path(tempfile.mkdtemp(prefix=f"{image_name}_staging_"))
    try:
        flat_dir = staging_dir / "lib" / "modules" / kernel_version
        flat_dir.mkdir(parents=True, exist_ok=True)

        modules_copied = 0
        for name in modules:
            found = list(base_modules_dir.rglob(name))
            if not found:
                log_message(f"WARNING: Module not found: {name}")
                continue
            dst = flat_dir / name
            shutil.copy(found[0], dst)

            if sign_modules:
                if not all(x.is_file() for x in [sign_tool, key_pem, key_x509]):
                    log_message("ERROR: Missing signing tool or keys")
                    sys.exit(1)
                result = subprocess.run(
                    [str(sign_tool), "sha1", str(key_pem), str(key_x509), str(dst)]
                )
                if result.returncode != 0:
                    log_message(f"ERROR: Failed to sign {name}")
                    sys.exit(1)

            modules_copied += 1

        if modules_copied == 0:
            log_message("ERROR: No modules copied – check module list or install path")
            sys.exit(1)

        tools = _require_tools(Path(KERNELBUILD_TOOLS_PATH), "mkfs.erofs", "depmod")

        run_cmd(f"{tools['depmod']} -b {staging_dir} {kernel_version}", fatal_on_error=True)

        # Flatten lib/modules/<version>/ → lib/modules/
        flat_mod_root = staging_dir / "lib" / "modules"
        for item in flat_dir.iterdir():
            shutil.move(str(item), flat_mod_root / item.name)
        shutil.rmtree(flat_dir)

        _rewrite_modules_dep(flat_mod_root / "modules.dep", mount_prefix)
        _copy_builtin_files(base_modules_dir / kernel_version, flat_mod_root)
        _write_modules_load_and_order(flat_mod_root, modules)

        fc_dir  = ROOT_DIR / "sepolicy"
        fc_file = fc_dir / f"{image_name}_file_contexts"

        run_cmd(
            f"{tools['mkfs.erofs']} "
            f"-z lz4hc,9 -T 0 "
            f"--mount-point {mount_prefix.strip('/')} "
            f"--file-contexts {fc_file} "
            f"{final_img} {staging_dir}",
            fatal_on_error=True,
        )

    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def build_vendorboot_image():
    """
    Assemble vendor_boot.img (header v4) using mkbootimg.

    Combines vendor ramdisk fragments (platform, dlkm, recovery),
    a DTB image, and a vendor bootconfig file.

    Requires:
        - dtb.img from --create-dtbo-images
        - vendor_ramdisk_dlkm.cpio.lz4 from --build-vendor-ramdisk-dlkm
    """
    final_img  = DIST_DIR / "vendor_boot.img"
    parts_dir  = ROOT_DIR / "vb_fragments"

    vendor_ramdisk_dlkm     = DIST_DIR / "vendor_ramdisk_dlkm.cpio.lz4"
    dtb_path                = DIST_DIR / "dtb.img"
    vendor_ramdisk_platform = parts_dir / "vendor_ramdisk_platform.lz4"
    vendor_ramdisk_recovery = parts_dir / "vendor_ramdisk_recovery.lz4"

    staging_dir = Path(tempfile.mkdtemp(prefix="vendor_boot_staging_"))
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        bootconfig_file = staging_dir / "vendor_bootconfig.txt"
        bootconfig_file.write_text(
            "buildtime_bootconfig=enable\n"
            "androidboot.serialconsole=0\n"
        )

        for f in [vendor_ramdisk_platform, vendor_ramdisk_recovery,
                  vendor_ramdisk_dlkm, dtb_path]:
            if not f.exists():
                log_message(f"ERROR: Missing required file: {f}")
                sys.exit(1)

        run_cmd(
            f"{MKBOOT_PATH / 'mkbootimg.py'} "
            f"--vendor_bootconfig {bootconfig_file} "
            f"--vendor_cmdline \"bootconfig loop.max_part=7\" "
            f"--header_version 4 "
            f"--dtb {dtb_path} "
            f"--pagesize 2048 "
            f"--ramdisk_name platform --ramdisk_type platform "
            f"--vendor_ramdisk_fragment {vendor_ramdisk_platform} "
            f"--ramdisk_name dlkm --ramdisk_type dlkm "
            f"--vendor_ramdisk_fragment {vendor_ramdisk_dlkm} "
            f"--ramdisk_name recovery --ramdisk_type recovery "
            f"--vendor_ramdisk_fragment {vendor_ramdisk_recovery} "
            f"--vendor_boot {final_img}",
            fatal_on_error=True,
        )

        if not final_img.exists():
            log_message("ERROR: vendor_boot.img was not created")
            sys.exit(1)

    finally:
        log_message(f"Cleaning up temporary directory: {staging_dir}")
        shutil.rmtree(staging_dir, ignore_errors=True)


def sign_partition_image(image_path: Path, partition_name: str):
    """
    Sign a partition image with AVBTool.
    Uses add_hash_footer for boot/vendor_boot, add_hashtree_footer for others.
    """
    avbtool  = KERNELBUILD_TOOLS_PATH / "avbtool"
    key_path = MKBOOT_PATH / "gki/testdata/testkey_rsa4096.pem"

    missing = [
        label for label, ok in [
            ("avbtool", avbtool.exists()),
            ("image",   image_path.exists()),
            ("key",     key_path.exists()),
        ] if not ok
    ]
    if missing:
        log_message(f"ERROR: Required component(s) missing: {', '.join(missing)}")
        sys.exit(1)

    log_message(f"Signing {partition_name}.img with AVBTool...")

    base_cmd = (
        f"{avbtool} add_hash_footer"
        if partition_name in {"boot", "vendor_boot"}
        else f"{avbtool} add_hashtree_footer"
    )
    common_args = (
        f"--image {image_path} "
        f"--partition_name {partition_name} "
        f"--key {key_path} "
        f"--algorithm SHA256_RSA4096"
    )

    if partition_name in {"boot", "vendor_boot", "dtbo", "init_boot"}:
        partition_sizes = {
            "boot":        67_108_864,
            "vendor_boot": 67_108_864,
            "dtbo":         8_388_608,
            "init_boot":   16_777_216,
        }
        padded_size = partition_sizes.get(partition_name) or (
            math.ceil((image_path.stat().st_size + 128 * 1024) / 4096) * 4096
        )
        extra = f"--partition_size {padded_size}"
        if partition_name not in {"boot", "vendor_boot"}:
            extra += " --do_not_generate_fec --hash_algorithm sha256"
    else:
        extra = "--do_not_generate_fec --hash_algorithm sha256"

    run_cmd(f"{base_cmd} {common_args} {extra}", fatal_on_error=True)
    log_message(f"{partition_name}.img signed successfully")

# ─── Defconfig helpers ────────────────────────────────────────────────────────

def find_defconfig(kernel_dir, defconfig=KERNEL_DEFCONFIG) -> Optional[str]:
    """Return the defconfig path string, or None if it doesn't exist."""
    path = f"{kernel_dir}/arch/arm64/configs/{defconfig}"
    if os.path.exists(path):
        return path
    log_message(f"[!] Defconfig not found: {path}")
    return None


def set_config(content: str, config: str, enabled: bool = True) -> str:
    """Enable or disable a single Kconfig symbol in defconfig content."""
    value    = f"{config}=y"
    disabled = f"# {config} is not set"

    if enabled:
        if value in content:
            return content
        if disabled in content:
            return content.replace(disabled, value)
        return content + f"\n{value}\n"
    else:
        if disabled in content:
            return content
        if value in content:
            return content.replace(value, disabled)
        return content + f"\n{disabled}\n"

# ─── Patching ─────────────────────────────────────────────────────────────────

def apply_patch(patch_file, working_dir) -> bool:
    """
    Apply a unified diff patch with patch -p1.

    Returns:
        True on success, False on failure.
    """
    working_dir = Path(working_dir)

    if not working_dir.exists():
        log_message(f"[!] Working directory does not exist: {working_dir}")
        return False
    if not Path(patch_file).exists():
        log_message(f"[!] Patch file does not exist: {patch_file}")
        return False

    result = subprocess.run(
        ["patch", "-p1", "-i", patch_file],
        cwd=str(working_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log_message(f"[!] Patch failed: {patch_file}")
        log_message(result.stdout)
        log_message(result.stderr)
    else:
        log_message(f"[+] Patch applied: {patch_file}")
    return result.returncode == 0


def add_droidspaces(add_droidspaces: int = 0):
    """
    Download and apply DroidSpaces KABI patches, then enable the required
    kernel configs in the defconfig.

    Args:
        add_droidspaces: Combination key (see DROIDSPACES_COMBINATIONS).
    """
    if not add_droidspaces:
        log_message("add_droidspaces is 0!")
        return

    if add_droidspaces not in DROIDSPACES_COMBINATIONS:
        log_message(f"[!] Unknown droidspaces combination: {add_droidspaces}")
        log_message(f"    Valid values: {sorted(DROIDSPACES_COMBINATIONS)}")
        return

    defconfig_path = find_defconfig(KERNEL_SOURCE_DIR, KERNEL_DEFCONFIG)
    if not defconfig_path:
        log_message("[!] Could not find defconfig")
        return

    log_message("Setting up droidspaces...")

    setup_dst = PREBUILTS_BASE_DIR / "droidspace"
    if setup_dst.exists():
        shutil.rmtree(str(setup_dst))
    setup_dst.mkdir(parents=True, exist_ok=True)

    downloader = _make_downloader()
    if downloader is None:
        log_message("[!] ERROR: wget or curl not found")
        return

    for filename in DROIDSPACES_PATCHES.values():
        downloader(f"{DROIDSPACES_BASE_URL}/{filename}", setup_dst / filename)

    for patch_id in DROIDSPACES_COMBINATIONS[add_droidspaces]:
        filename   = DROIDSPACES_PATCHES[patch_id]
        patch_file = setup_dst / filename
        shutil.move(str(patch_file), str(KERNEL_SOURCE_DIR))
        log_message(f"Applying droidspaces patch {patch_id}: {filename}")
        apply_patch(KERNEL_SOURCE_DIR / filename, KERNEL_SOURCE_DIR)

    with open(defconfig_path) as f:
        content = f.read()
    for config in DROIDSPACES_KERNEL_CONFIGS:
        content = set_config(content, config, enabled=True)
        log_message(f"[+] Enabled {config}")
    with open(defconfig_path, "w") as f:
        f.write(content)

    log_message("[+] DroidSpaces setup complete")


def enable_kernel_configs(kernel_dir):
    """Enable KernelSU and SUSFS configs in the defconfig."""
    required_configs = [
        "CONFIG_KSU",
        "CONFIG_KSU_SUSFS",
    ]
    optional_configs = [
        "CONFIG_KSU_SUSFS_HAS_MAGIC_MOUNT",
        "CONFIG_KSU_SUSFS_SUS_PATH",
        "CONFIG_KSU_SUSFS_SUS_MOUNT",
        "CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT",
        "CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT",
        "CONFIG_KSU_SUSFS_SUS_KSTAT",
        "CONFIG_KSU_SUSFS_TRY_UMOUNT",
        "CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT",
        "CONFIG_KSU_SUSFS_SPOOF_UNAME",
        "CONFIG_KSU_SUSFS_ENABLE_LOG",
        "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS",
        "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG",
        "CONFIG_KSU_SUSFS_OPEN_REDIRECT",
    ]

    defconfig_path = find_defconfig(kernel_dir)
    if not defconfig_path:
        log_message("[!] Could not find defconfig")
        return False

    log_message(f"[+] Using defconfig: {defconfig_path}")

    with open(defconfig_path) as f:
        content = f.read()

    for config in required_configs:
        content = set_config(content, config, enabled=True)
        log_message(f"[+] Enabled {config}")
    for config in optional_configs:
        content = set_config(content, config, enabled=True)
        log_message(f"[+] Enabled optional {config}")

    with open(defconfig_path, "w") as f:
        f.write(content)

    log_message("[+] Defconfig updated successfully")
    return True


def remove_protected_exports(kernel_dir):
    """Remove GKI protected-exports files (required for out-of-tree symbols)."""
    for path_str in [
        f"{kernel_dir}/common/android/abi_gki_protected_exports_aarch64",
        f"{kernel_dir}/common/android/abi_gki_protected_exports_x86_64",
    ]:
        if os.path.exists(path_str):
            os.remove(path_str)
            log_message(f"[-] Removed {path_str}")
        else:
            log_message(f"[~] Skipping {path_str} (not found)")


def fix_sus_su_constants(kernel_dir):
    """Append SUS_SU working-mode constants to susfs_def.h if not already present."""
    susfs_def = Path(kernel_dir) / "include" / "linux" / "susfs_def.h"
    if not susfs_def.exists():
        log_message(f"[!] susfs_def.h not found: {susfs_def}")
        return False

    content = susfs_def.read_text()
    if "SUS_SU_DISABLED" in content:
        log_message("[~] SUS_SU constants already defined, skipping")
        return True

    content += (
        "\n/* SUS_SU working modes */\n"
        "#define SUS_SU_DISABLED 0\n"
        "#define SUS_SU_WITH_KPROBES 1\n"
        "#define SUS_SU_WITH_HOOKS 2\n"
    )
    susfs_def.write_text(content)
    log_message("[+] Added SUS_SU constants to susfs_def.h")
    return True


def fix_open_c(kernel_dir):
    """Manually apply the SUSFS open-redirect patch to fs/open.c."""
    open_c = Path(kernel_dir) / "fs" / "open.c"
    if not open_c.exists():
        log_message(f"[!] fs/open.c not found: {open_c}")
        return False

    content = open_c.read_text()
    if "retry:\n#endif // #ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT" in content:
        log_message("[~] fs/open.c already patched, skipping")
        return True

    # Insert retry label after get_unused_fd_flags
    old1 = "\tfd = get_unused_fd_flags(how->flags);\n\tif (fd >= 0) {"
    new1 = (
        "\tfd = get_unused_fd_flags(how->flags);\n"
        "#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT\n"
        "retry:\n"
        "#endif // #ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT\n"
        "\tif (fd >= 0) {"
    )
    if old1 not in content:
        log_message("[!] Could not find retry-label insertion point in fs/open.c")
        return False
    content = content.replace(old1, new1)

    # Insert open-redirect check block after do_filp_open
    old2 = (
        "\t\tstruct file *f = do_filp_open(dfd, tmp, &op);\n"
        "#ifdef CONFIG_SECURITY_DEFEX"
    )
    new2 = (
        "\t\tstruct file *f = do_filp_open(dfd, tmp, &op);\n"
        "\n"
        "#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT\n"
        "\t\tif (!is_inode_open_redirect && f && !IS_ERR(f)) {\n"
        "\t\t\tstruct inode *inode = file_inode(f);\n"
        "\t\t\tif (SUSFS_IS_INODE_OPEN_REDIRECT_WITHOUT_UID_CHECK(inode)) {\n"
        "\t\t\t\tfake_filename = susfs_open_redirect_spoof_do_sys_openat(inode);\n"
        "\t\t\t\tif (fake_filename && !IS_ERR(fake_filename)) {\n"
        "\t\t\t\t\tis_inode_open_redirect = true;\n"
        "\t\t\t\t\tfilp_close(f, NULL);\n"
        "\t\t\t\t\tputname(tmp);\n"
        "\t\t\t\t\ttmp = fake_filename;\n"
        "\t\t\t\t\tgoto retry;\n"
        "\t\t\t\t}\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "#endif // #ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT\n"
        "\n"
        "#ifdef CONFIG_SECURITY_DEFEX"
    )
    if old2 not in content:
        log_message("[!] Could not find open-redirect insertion point in fs/open.c")
        return False
    content = content.replace(old2, new2)

    open_c.write_text(content)
    log_message("[+] Successfully patched fs/open.c")
    return True


def include_susfs_patches(patch_susfs: bool = False):
    """
    Copy SUSFS sources into the kernel tree and apply the kernel patch.
    Falls back to manual fixes if the patch does not apply cleanly.
    """
    if not patch_susfs:
        log_message("Patching with SUSFS is not enabled.")
        return

    log_message("Starting SUSFS patching...")

    kernel_patches = list((SUSFS_PATH / "kernel_patches").glob("50_add_susfs_in_gki-*.patch"))
    if not kernel_patches:
        log_message("[!] No susfs kernel patch found in SUSFS_PATH/kernel_patches/")
        return

    patch_src = kernel_patches[0]
    patch_dst = KERNEL_SOURCE_DIR / patch_src.name
    shutil.copy(patch_src, patch_dst)
    log_message(f"[+] Copied kernel patch to {patch_dst}")

    shutil.copytree(SUSFS_PATH / "kernel_patches" / "fs",
                    KERNEL_SOURCE_DIR / "fs", dirs_exist_ok=True)
    log_message("[+] Copied fs/")

    shutil.copytree(SUSFS_PATH / "kernel_patches" / "include" / "linux",
                    KERNEL_SOURCE_DIR / "include" / "linux", dirs_exist_ok=True)
    log_message("[+] Copied include/linux/")

    fix_sus_su_constants(KERNEL_SOURCE_DIR)

    log_message("[+] Applying kernel SUSFS patch...")
    if not apply_patch(str(patch_dst), KERNEL_SOURCE_DIR):
        log_message("[!] Kernel SUSFS patch had failures, applying manual fixes...")
        fix_open_c(KERNEL_SOURCE_DIR)

    enable_kernel_configs(KERNEL_SOURCE_DIR)
    remove_protected_exports(KERNEL_SOURCE_DIR)
    log_message("[+] SUSFS patching completed")


def patch_with_sukisu(install_sukisu: bool = False):
    """
    Integrate SukiSU Ultra into the kernel source via its setup.sh script,
    then apply the syscall hooks patch.
    """
    global USING_MANUAL_HOOK
    USING_MANUAL_HOOK = True

    if not install_sukisu:
        log_message("Skipping SukiSU Ultra installation...")
        return

    log_message("Patching with SukiSU Ultra...")

    for dirname in ["KernelSU", "KernelSU-Next"]:
        stale = KERNEL_SOURCE_DIR / dirname
        if stale.exists():
            shutil.rmtree(stale)
            log_message(f"[-] Removed stale {dirname}/")

    setup_dst = KERNEL_SOURCE_DIR / "setup.sh"
    setup_url = URLS_CONFIG["SukiSU_Ultra"]["setup_url"]
    patch_url = URLS_CONFIG["SukiSU_Ultra"]["hooks_patch_url"]

    log_message(f"Downloading SukiSU Ultra setup.sh from {setup_url}...")
    downloader = _make_downloader()
    if downloader is None:
        log_message("ERROR: wget or curl not found")
        return
    downloader(setup_url, setup_dst)

    result = subprocess.run(
        ["bash", str(setup_dst), "susfs-test"],
        capture_output=True, text=True,
        cwd=KERNEL_SOURCE_DIR,
    )
    if result.returncode != 0:
        log_message(f"setup.sh failed:\n{result.stdout}\n{result.stderr}")
        return

    log_message(f"setup.sh completed: {result.stdout}")

    if not SUKISU_PATH.exists():
        log_message("[!] KernelSU directory not found after setup.sh ran")
        return

    remote_result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(SUKISU_PATH),
        capture_output=True, text=True,
    )
    remote = remote_result.stdout.strip()
    log_message(f"[+] KSU remote: {remote}")
    if "SukiSU-Ultra" not in remote:
        log_message(f"[!] Wrong repo cloned: expected SukiSU-Ultra, got: {remote}")
        return

    hooks_patch = KERNEL_SOURCE_DIR / "syscall_hooks.patch"
    log_message(f"Downloading syscall hooks patch from {patch_url}...")
    downloader(patch_url, hooks_patch)

    if not apply_patch(hooks_patch, KERNEL_SOURCE_DIR):
        log_message("[!] syscall_hooks.patch failed to apply (continuing anyway)")

    log_message("[+] SukiSU Ultra installed successfully")


def patch_with_ksun(install_ksun: bool = False):
    """
    Integrate KernelSU-Next into the kernel source via its setup.sh script.
    """
    if not install_ksun:
        log_message("Skipping KernelSU-Next installation...")
        return

    log_message("Patching with KernelSU-Next...")

    for dirname in ["KernelSU", "KernelSU-Next"]:
        stale = KERNEL_SOURCE_DIR / dirname
        if stale.exists():
            shutil.rmtree(stale)
            log_message(f"[-] Removed stale {dirname}/")

    setup_dst = KERNEL_SOURCE_DIR / "setup.sh"
    url = URLS_CONFIG["KernelSU_Next"]["setup_url"]

    log_message(f"Downloading KernelSU-Next setup.sh from {url}...")
    downloader = _make_downloader()
    if downloader is None:
        log_message("ERROR: wget or curl not found")
        return
    downloader(url, setup_dst)

    result = subprocess.run(
        ["bash", str(setup_dst)],
        capture_output=True, text=True,
        cwd=KERNEL_SOURCE_DIR,
    )
    if result.returncode != 0:
        log_message(f"setup.sh failed:\n{result.stdout}\n{result.stderr}")
        return

    log_message(f"setup.sh completed: {result.stdout}")
    log_message("[+] KernelSU-Next installed successfully")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Android kernel build script for Samsung Galaxy A55 (a55x / exynos s5e8845)",
        epilog=dedent("""
            Examples:
              %(prog)s
                  Build using all CPU cores.

              %(prog)s --clean
                  Clean previous artifacts, then build.

              %(prog)s -j8
                  Build using 8 parallel jobs.

              %(prog)s --clean -j$(nproc)
                  Clean and build with all cores.

              %(prog)s --build-all
                  Full build: kernel + dtbo + boot + vendor_boot + dlkm + sign.

              %(prog)s --clean --build-all
                  Clean, then run a full build.

              %(prog)s --install-ksun
                  Integrate KernelSU-Next. Can be combined with any other flag.

              %(prog)s --install-sukisu
                  Integrate SukiSU Ultra instead of KernelSU-Next.

              %(prog)s --install-ksun --patch-susfs
                  Integrate KernelSU-Next and apply SUSFS patches automatically.
                  --install-ksun or --install-sukisu is required for --patch-susfs.

              %(prog)s --install-ksun --add-droidspaces
                  Apply DroidSpaces KABI patch combination 1 (default).
                  Root integration (--install-ksun or --install-sukisu) is required.

              %(prog)s --install-ksun --add-droidspaces=12
                  Apply DroidSpaces patch combinations 1 and 2.
                  Valid values: 1, 2, 3, 12, 13, 23, 123.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--clean", action="store_true",
        help="Clean previous build artifacts before starting")

    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count(),
        help=f"Number of parallel build jobs (default: {os.cpu_count()})")

    parser.add_argument("--skip-prebuilt-update", action="store_true",
        help="Skip updating or re-downloading prebuilts that are already present")

    parser.add_argument("--extra-local-version", action="store_true",
        help="Inject BRANCH and KMI_GENERATION into the environment for setlocalversion")

    parser.add_argument("--create-dtbo-images", action="store_true",
        help="Build dtbo.img and dtb.img from compiled DTBO/DTB files")

    parser.add_argument("--create-boot-image", action="store_true",
        help="Build boot.img from the compiled kernel Image")

    parser.add_argument("--build-vendor-ramdisk-dlkm", action="store_true",
        help="Build vendor_ramdisk_dlkm.cpio.lz4")

    parser.add_argument("--build-vendor-boot-image", action="store_true",
        help="Build vendor_boot.img (auto-enables --create-dtbo-images and "
             "--build-vendor-ramdisk-dlkm if not set)")

    parser.add_argument("--build-dlkm-image", action="store_true",
        help="Build system_dlkm.img and vendor_dlkm.img in EROFS format")

    parser.add_argument("--sign-images", action="store_true",
        help="Sign all built partition images with AVBTool")

    parser.add_argument("--flashable-zip", action="store_true",
        help="Package the built kernel Image into a flashable AnyKernel3 ZIP")

    parser.add_argument("--build-all", action="store_true",
        help="Enable every build step: dtbo, boot, vendor_boot, dlkm, sign, "
             "KernelSU-Next, and SUSFS patching")

    parser.add_argument("--install-ksun", action="store_true",
        help="Integrate KernelSU-Next into the kernel source before building")

    parser.add_argument("--install-sukisu", action="store_true",
        help="Integrate SukiSU Ultra into the kernel source (mutually exclusive "
             "with --install-ksun in practice)")

    parser.add_argument("--patch-susfs", action="store_true",
        help="Apply SUSFS patches automatically. Requires --install-ksun or "
             "--install-sukisu")

    parser.add_argument("--add-droidspaces",
        type=int, nargs="?", const=1, default=None,
        metavar="COMBO",
        help="Apply DroidSpaces KABI patches and enable required kernel configs. "
             "COMBO selects which patch set to apply: "
             "1=6.7/8, 2=3/4/5, 3=1/2/3, 12=1+2, 13=1+3, 23=2+3, 123=all. "
             "Omitting COMBO defaults to 1. "
             "Requires --install-ksun or --install-sukisu.")

    args = parser.parse_args()

    if args.build_all:
        log_message("--build-all: enabling all build options and KernelSU-Next")
        args.extra_local_version      = True
        args.create_dtbo_images       = True
        args.create_boot_image        = True
        args.flashable_zip            = True
        args.build_vendor_ramdisk_dlkm = True
        args.build_vendor_boot_image  = True
        args.build_dlkm_image         = True
        args.sign_images              = True
        args.install_ksun             = True
        args.patch_susfs              = True
        args.add_droidspaces          = 1   # default combination

    log_message("Starting Android kernel build process...")

    try:
        setup_environment(skip_prebuilt_update=args.skip_prebuilt_update)
        validate_prebuilts()

        if args.clean:
            clean_build_artifacts()

        install_modules = (
            args.build_vendor_ramdisk_dlkm or
            args.build_vendor_boot_image   or
            args.build_dlkm_image
        )

        if DIST_DIR.exists():
            log_message(f"Cleaning DIST_DIR: {DIST_DIR}")
            shutil.rmtree(DIST_DIR, ignore_errors=True)

        # ── Root integration ──────────────────────────────────────────────────
        if args.install_ksun:
            patch_with_ksun(install_ksun=True)

        if args.install_sukisu:
            patch_with_sukisu(install_sukisu=True)

        # ── SUSFS ─────────────────────────────────────────────────────────────
        if args.patch_susfs and not (args.install_ksun or args.install_sukisu):
            log_message("WARNING: --patch-susfs requires --install-ksun or --install-sukisu; skipping")
            args.patch_susfs = False

        if args.patch_susfs and args.install_ksun:
            include_susfs_patches(patch_susfs=True)

        if args.patch_susfs and args.install_sukisu:
            log_message("NOTE: SUSFS + SukiSU Ultra patching is currently suspended")

        # ── DroidSpaces ───────────────────────────────────────────────────────
        if args.add_droidspaces is not None:
            if not (args.install_ksun or args.install_sukisu):
                log_message("WARNING: --add-droidspaces requires root integration; skipping")
            else:
                add_droidspaces(add_droidspaces=args.add_droidspaces)

        # ── Kernel compile ────────────────────────────────────────────────────
        if args.extra_local_version:
            version_env = get_version_env()
            log_message(
                f"Local version env: BRANCH={version_env['BRANCH']}, "
                f"KMI_GENERATION={version_env['KMI_GENERATION']}"
            )
            build_kernel(args.jobs, version_env, install_modules=install_modules)
        else:
            build_kernel(args.jobs, install_modules=install_modules)

        if args.flashable_zip:
            create_flash_zip()

        if args.create_dtbo_images or args.build_vendor_boot_image:
            build_dtbo_images()

        if args.create_boot_image:
            build_boot_image()

        # ── Vendor ramdisk DLKM (standalone) ─────────────────────────────────
        if args.build_vendor_ramdisk_dlkm and not args.build_vendor_boot_image:
            mk_vendor_rd_dlkm(
                mount_prefix="",
                module_early_list_file=VENDOR_RAMDISK_DLKM_EARLY_MODULES_FILE,
                module_list_file=VENDOR_RAMDISK_DLKM_MODULES_FILE,
            )

        # ── Vendor boot ───────────────────────────────────────────────────────
        if args.build_vendor_boot_image:
            if not args.create_dtbo_images:
                log_message("Auto-enabling --create-dtbo-images (required for vendor_boot.img)")
            if not args.build_vendor_ramdisk_dlkm:
                log_message("Auto-enabling --build-vendor-ramdisk-dlkm (required for vendor_boot.img)")

            build_dtbo_images()
            mk_vendor_rd_dlkm(
                mount_prefix="",
                module_early_list_file=VENDOR_RAMDISK_DLKM_EARLY_MODULES_FILE,
                module_list_file=VENDOR_RAMDISK_DLKM_MODULES_FILE,
            )
            build_vendorboot_image()

        # ── DLKM images ───────────────────────────────────────────────────────
        if args.build_dlkm_image:
            build_dlkm_image("system_dlkm", None,                   "/system_dlkm", sign_modules=True)
            build_dlkm_image("vendor_dlkm", VENDOR_DLKM_MODULES_FILE, "/vendor_dlkm", sign_modules=False)

        # ── Signing ───────────────────────────────────────────────────────────
        if args.sign_images:
            images = [
                ("dtbo",        DIST_DIR / "dtbo.img",        args.create_dtbo_images),
                ("boot",        DIST_DIR / "boot.img",        args.create_boot_image),
                ("system_dlkm", DIST_DIR / "system_dlkm.img", args.build_dlkm_image),
                ("vendor_dlkm", DIST_DIR / "vendor_dlkm.img", args.build_dlkm_image),
                ("vendor_boot", DIST_DIR / "vendor_boot.img", args.build_vendor_boot_image),
            ]
            signed_any = False
            for name, path, requested in images:
                if path.exists():
                    if requested:
                        sign_partition_image(path, name)
                        signed_any = True
                    else:
                        log_message(f"SKIP: {name}.img exists but was not requested")
                elif requested:
                    log_message(f"MISS: {name}.img was requested but not built")

            if not signed_any:
                log_message("ERROR: --sign-images given but no image found to sign")
                sys.exit(1)

    except SystemExit:
        log_message("Build terminated due to a fatal error")
        sys.exit(1)
    except Exception as e:
        log_message(f"CRITICAL: Unhandled exception: {e}")
        sys.exit(1)

    log_message("Android kernel build completed successfully.")


if __name__ == "__main__":
    main()
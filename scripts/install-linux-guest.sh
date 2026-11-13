#!/usr/bin/env bash
set -Eeuo pipefail

REPO="https://github.com/MrOz59/Hermes-KMS.git"
COMMIT="143209286852430f5990a1fc8a95454b16bdea4c"
MODE=${IDD_MODE:-1920x1080@60}
COMPOSITOR=drm
GUEST_ACK=0

fail() { printf 'IDD installer: %s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
    case "$arg" in
        --guest) GUEST_ACK=1 ;;
        --compositor=drm) COMPOSITOR=drm ;;
        --compositor=hyprland) COMPOSITOR=hyprland ;;
        *) fail "unknown option: $arg (use --guest and optionally --compositor=drm|hyprland)" ;;
    esac
done
[[ $EUID -eq 0 ]] || fail 'run with sudo: sudo bash ./scripts/install-linux-guest.sh --guest'
[[ $GUEST_ACK -eq 1 ]] || fail 'refusing to modify this OS without --guest. Run this ONLY inside the VM: sudo bash ./scripts/install-linux-guest.sh --guest'

# systemd-detect-virt alone is not sufficient: some custom VFIO/QEMU setups
# return "none". Also inspect DMI and PCI vendor/device strings.
virt="$(systemd-detect-virt --vm 2>/dev/null || true)"
[[ "$virt" == "none" ]] && virt=""
dmi="$( { cat /sys/class/dmi/id/sys_vendor 2>/dev/null; cat /sys/class/dmi/id/product_name 2>/dev/null; } | tr '[:upper:]' '[:lower:]' )"
pci_hint=''
if command -v lspci >/dev/null 2>&1; then
    pci_hint="$(lspci -nnk 2>/dev/null | tr '[:upper:]' '[:lower:]' | grep -m1 -E 'qemu virtual machine|vmware|virtualbox|kvm|xen|bochs' || true)"
fi
if [[ -z "$virt" && ! "$dmi" =~ (qemu|kvm|vmware|virtualbox|xen|bochs|virtual) && -z "$pci_hint" ]]; then
    fail "no hypervisor marker detected (systemd-detect-virt=$([ -n "$virt" ] && echo "$virt" || echo none)). This installer only modifies the guest. Verify you are inside the VM; if this is a custom hypervisor that hides all VM identifiers, review the script and add your own marker before running it."
fi

[[ "$MODE" =~ ^([0-9]{3,5})x([0-9]{3,5})@([0-9]{1,3})$ ]] || fail "invalid IDD_MODE '$MODE' (expected WIDTHxHEIGHT@HZ)"
width=${BASH_REMATCH[1]}
height=${BASH_REMATCH[2]}
refresh=${BASH_REMATCH[3]}
(( width >= 640 && width <= 7680 && height >= 480 && height <= 4320 && refresh >= 24 && refresh <= 240 )) || \
    fail "IDD_MODE '$MODE' is outside safe defaults (640x480..7680x4320, 24..240 Hz)"

command -v nvidia-smi >/dev/null 2>&1 || fail 'nvidia-smi not found. Install the NVIDIA driver inside the guest first.'
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || fail 'NVIDIA GPU is not accessible inside this OS'
[[ -r /sys/module/nvidia_drm/parameters/modeset ]] || fail 'nvidia_drm is not loaded; enable NVIDIA DRM KMS in the guest first'
[[ "$(cat /sys/module/nvidia_drm/parameters/modeset)" == Y ]] || fail 'nvidia_drm modeset is not Y; enable NVIDIA DRM KMS in the guest first'

printf '\nThis installs a guest-local virtual display integration for %s.\n' "$COMPOSITOR"
printf 'It will modify ONLY the Linux OS on which this script is running.\n'
printf 'Do not run this on the hypervisor/host. No NVIDIA connector or host EDID is changed.\n'
printf 'Detected VM marker: %s\n' "${virt:-${pci_hint:-DMI marker}}"
printf 'Type INSTALL-IN-GUEST to continue: '
read -r answer
[[ "$answer" == INSTALL-IN-GUEST ]] || fail 'confirmation did not match; no changes made'

[[ ! -e /etc/idd/backend ]] ||
    fail "an Idd installation marker already exists at /etc/idd/backend. Use the existing installation or uninstall it from inside the guest first."

[[ -r /etc/os-release ]] || fail '/etc/os-release missing'
. /etc/os-release
KVER=$(uname -r)
KDIR="/lib/modules/$KVER/build"

verify_kernel_headers() {
    [[ -e "$KDIR/Makefile" ]] || return 1
    local header_release=''
    header_release=$(make -s -C "$KDIR" kernelrelease 2>/dev/null || true)
    [[ -n "$header_release" && "$header_release" == "$KVER" ]] ||
        fail "headers at $KDIR report kernelrelease '${header_release:-unavailable}', but the running kernel is '$KVER'. Boot the kernel matching the installed headers (or install exact matching headers), then rerun."
    return 0
}

install_arch_kernel_headers() {
    verify_kernel_headers && return 0

    local pkgbase='' headers_pkg=''
    if [[ -r "/usr/lib/modules/$KVER/pkgbase" ]]; then
        pkgbase=$(<"/usr/lib/modules/$KVER/pkgbase")
        headers_pkg="${pkgbase}-headers"
    elif [[ "$KVER" == *cachyos* ]]; then
        headers_pkg=linux-cachyos-headers
    elif [[ "$KVER" == *-zen* ]]; then
        headers_pkg=linux-zen-headers
    elif [[ "$KVER" == *-hardened* ]]; then
        headers_pkg=linux-hardened-headers
    elif [[ "$KVER" == *-lts* ]]; then
        headers_pkg=linux-lts-headers
    else
        headers_pkg=linux-headers
    fi

    pacman -Si "$headers_pkg" >/dev/null 2>&1 ||
        fail "cannot find matching headers package '$headers_pkg' for kernel $KVER. Install the headers package built for this exact kernel, then rerun."
    pacman -S --needed --noconfirm "$headers_pkg"
    verify_kernel_headers ||
        fail "headers package '$headers_pkg' did not provide matching build files for running kernel $KVER. The running kernel and installed headers must match exactly."
}

install_debian_kernel_headers() {
    verify_kernel_headers && return 0

    local headers_pkg="linux-headers-$KVER"
    if ! apt-cache show "$headers_pkg" 2>/dev/null | grep '^Package: ' >/dev/null; then
        headers_pkg="custom-kernel-headers-$KVER"
    fi
    apt-cache show "$headers_pkg" 2>/dev/null | grep '^Package: ' >/dev/null ||
        fail "APT has no headers package for running kernel $KVER. Install the exact matching kernel headers from the kernel vendor, then rerun."
    apt-get install -y "$headers_pkg"
    verify_kernel_headers ||
        fail "headers package '$headers_pkg' did not provide matching build files for running kernel $KVER. The running kernel and installed headers must match exactly."
}

case "${ID:-}" in
    arch|cachyos|endeavouros|manjaro)
        command -v pacman >/dev/null 2>&1 || fail 'pacman not found'
        pacman -S --needed --noconfirm pciutils
        if [[ "$COMPOSITOR" == drm ]]; then
            pacman -S --needed --noconfirm base-devel dkms git pahole
            install_arch_kernel_headers
        else
            pacman -S --needed --noconfirm wlr-randr
        fi
        ;;
    debian|ubuntu)
        command -v apt-get >/dev/null 2>&1 || fail 'apt-get not found'
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y pciutils
        if [[ "$COMPOSITOR" == drm ]]; then
            apt-get install -y build-essential dkms git dwarves
            install_debian_kernel_headers
        else
            apt-get install -y wlr-randr
        fi
        ;;
    *)
        fail "unsupported distribution ID='${ID:-unknown}'. Supported: Arch/CachyOS derivatives and Debian/Ubuntu."
        ;;
esac

if [[ "$COMPOSITOR" == drm ]]; then
    verify_kernel_headers || fail "kernel headers missing or mismatched at $KDIR for running kernel $KVER"

    # Upstream's dkms-install target removes prior Hermes-KMS DKMS registrations
    # before installing. Do not silently take over a driver another package owns.
    shopt -s nullglob
    existing_hermes_sources=(/usr/src/hermes-kms-*)
    shopt -u nullglob
    if [[ -d /sys/module/hermes_kms ]] ||
       { command -v modinfo >/dev/null 2>&1 && modinfo hermes_kms >/dev/null 2>&1; } ||
       { command -v dkms >/dev/null 2>&1 && dkms status 2>/dev/null | grep '^hermes-kms/' >/dev/null; } ||
       [[ -e /usr/lib/modules-load.d/hermes-kms.conf ]] ||
       [[ -e /usr/lib/modprobe.d/hermes-kms.conf ]] ||
       (( ${#existing_hermes_sources[@]} > 0 )); then
        fail "Hermes-KMS files or module are already installed. This installer will not overwrite an existing DKMS/package installation; remove or upgrade that installation explicitly first."
    fi

    # Record ownership before any DKMS writes so the scoped uninstaller can
    # remove a partially completed install if fetching or building fails.
    install -d -m0755 /etc/idd
    printf '%s\n' drm > /etc/idd/backend
    printf '%s\n' 0.4.1 > /etc/idd/hermes-kms-version

    workdir=$(mktemp -d)
    cleanup() { rm -rf -- "$workdir"; }
    trap cleanup EXIT

    git init "$workdir/Hermes-KMS"
    git -C "$workdir/Hermes-KMS" remote add origin "$REPO"
    git -C "$workdir/Hermes-KMS" fetch --depth 1 origin "$COMMIT"
    git -C "$workdir/Hermes-KMS" checkout --detach FETCH_HEAD
    make -C "$workdir/Hermes-KMS" dkms-install
    make -C "$workdir/Hermes-KMS" tools/hermes-kmsctl/hermes-kmsctl
    install -Dm0755 "$workdir/Hermes-KMS/tools/hermes-kmsctl/hermes-kmsctl" /usr/local/bin/idd-kmsctl
fi

# The DRM branch wrote its marker before DKMS installation so failed
# partial builds remain removable. Hyprland has no kernel module to build.
if [[ "$COMPOSITOR" == hyprland ]]; then
    install -d -m0755 /etc/idd
    printf '%s\n' hyprland > /etc/idd/backend
fi

install -Dm0755 "$(dirname "$0")/iddctl" /usr/local/bin/iddctl
install -Dm0755 "$(dirname "$0")/configure-compositor-gpus.sh" /usr/local/bin/idd-configure-compositor-gpus
install -d -m0755 /etc/idd
if [[ ! -e /etc/idd/virtual-display.conf ]]; then
    install -m0644 /dev/null /etc/idd/virtual-display.conf
    printf 'IDD_MODE=%s\n' "$MODE" > /etc/idd/virtual-display.conf
fi

if [[ "$COMPOSITOR" == drm ]]; then
    install -Dm0644 "$(dirname "$0")/../systemd/idd-virtual-display.service" /etc/systemd/system/idd-virtual-display.service
    systemctl daemon-reload
    systemctl enable --now idd-virtual-display.service
    /usr/local/bin/idd-configure-compositor-gpus drm
    printf '\nInstalled the guest-local Hermes-KMS DRM/KMS output.\n'
    printf 'NVIDIA is configured as the preferred renderer for KWin/Mutter and for Hyprland headless sessions; log out/in to apply it.\n'
    printf 'KDE Wayland: iddctl activate-kde (or enable HERMES-1 in Display Configuration)\n'
    printf 'GNOME Wayland: iddctl activate-gnome\n'
else
    /usr/local/bin/idd-configure-compositor-gpus hyprland
    printf '\nInstalled the Hyprland headless-output helper only; no Hermes-KMS module or system service was installed.\n'
    printf 'AQ_DRM_DEVICES is configured to use the passed-through NVIDIA GPU; log out/in to apply it.\n'
    printf 'Run iddctl hyprland-start as the logged-in Hyprland user (no sudo).\n'
fi

printf '\nConfigured mode: %s\n' "$MODE"
printf 'Check guest GPU/display devices: iddctl verify\n'
printf 'The virtual display exists only inside this guest.\n'


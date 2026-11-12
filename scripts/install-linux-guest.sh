#!/usr/bin/env bash
set -Eeuo pipefail

REPO="https://github.com/MrOz59/Hermes-KMS.git"
TAG="v0.4.0"
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

[[ -r /etc/os-release ]] || fail '/etc/os-release missing'
. /etc/os-release
case "${ID:-}" in
    arch|cachyos|endeavouros|manjaro)
        command -v pacman >/dev/null 2>&1 || fail 'pacman not found'
        if [[ "$COMPOSITOR" == drm ]]; then
            pacman -S --needed --noconfirm base-devel dkms git wlr-randr pciutils
        else
            pacman -S --needed --noconfirm wlr-randr pciutils
        fi
        ;;
    debian|ubuntu)
        command -v apt-get >/dev/null 2>&1 || fail 'apt-get not found'
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        if [[ "$COMPOSITOR" == drm ]]; then
            apt-get install -y build-essential dkms git "linux-headers-$(uname -r)" wlr-randr pciutils
        else
            apt-get install -y wlr-randr pciutils
        fi
        ;;
    *)
        fail "unsupported distribution ID='${ID:-unknown}'. Supported: Arch/CachyOS derivatives and Debian/Ubuntu."
        ;;
esac

if [[ "$COMPOSITOR" == drm ]]; then
    KDIR="/lib/modules/$(uname -r)/build"
    [[ -e "$KDIR/Makefile" ]] || fail "kernel headers missing at $KDIR. Install headers matching $(uname -r), reboot into that kernel, then rerun."

    workdir=$(mktemp -d)
    cleanup() { rm -rf -- "$workdir"; }
    trap cleanup EXIT

    git clone --depth 1 --branch "$TAG" "$REPO" "$workdir/Hermes-KMS"
    make -C "$workdir/Hermes-KMS" dkms-install
    make -C "$workdir/Hermes-KMS" tools/hermes-kmsctl/hermes-kmsctl
    install -Dm0755 "$workdir/Hermes-KMS/tools/hermes-kmsctl/hermes-kmsctl" /usr/local/bin/idd-kmsctl
fi

install -Dm0755 "$(dirname "$0")/iddctl" /usr/local/bin/iddctl
install -d -m0755 /etc/idd
if [[ ! -e /etc/idd/virtual-display.conf ]]; then
    install -m0644 /dev/null /etc/idd/virtual-display.conf
    printf 'IDD_MODE=%s\n' "$MODE" > /etc/idd/virtual-display.conf
fi

if [[ "$COMPOSITOR" == drm ]]; then
    install -Dm0644 "$(dirname "$0")/../systemd/idd-virtual-display.service" /etc/systemd/system/idd-virtual-display.service
    systemctl daemon-reload
    systemctl enable --now idd-virtual-display.service
    printf '\nInstalled the guest-local Hermes-KMS DRM/KMS output.\n'
    printf 'KDE Wayland: iddctl activate-kde (or enable HERMES-1 in Display Configuration)\n'
    printf 'GNOME Wayland: iddctl activate-gnome\n'
else
    printf '\nInstalled the Hyprland headless-output helper only; no Hermes-KMS module or system service was installed.\n'
    printf 'Add this to your Hyprland environment before starting Hyprland if the RTX is not already the selected renderer:\n'
    printf '  env = AQ_DRM_DEVICES,/dev/dri/card1\n'
    printf 'Use the card path that iddctl verify identifies as the NVIDIA device; card numbering can change after reboot.\n'
    printf 'Run iddctl hyprland-start as the logged-in Hyprland user (no sudo).\n'
fi

printf '\nConfigured mode: %s\n' "$MODE"
printf 'Check guest GPU/display devices: iddctl verify\n'
printf 'The virtual display exists only inside this guest.\n'

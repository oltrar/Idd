#!/usr/bin/env bash
set -Eeuo pipefail

MODE=${1:-drm}
RULE=/etc/udev/rules.d/61-idd-gpu-priority.rules
ENV_DIR=/etc/environment.d
ENV_FILE=$ENV_DIR/90-idd-virtual-display.conf
fail() { printf 'IDD GPU setup: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail 'must run as root'
[[ "$MODE" == drm || "$MODE" == hyprland ]] || fail 'usage: configure-compositor-gpus.sh drm|hyprland'

nvidia_card=''
nvidia_pci=''
nvidia_vendor=''
nvidia_device=''
for card in /sys/class/drm/card[0-9]*; do
    name=${card##*/}
    [[ "$name" =~ ^card[0-9]+$ ]] || continue
    [[ -e "$card/device/driver" ]] || continue
    driver=$(readlink -f "$card/device/driver" 2>/dev/null || true)
    [[ "${driver##*/}" == nvidia ]] || continue
    nvidia_card=$name
    pci_path=$(readlink -f "$card/device" 2>/dev/null || true)
    nvidia_pci=${pci_path##*/}
    nvidia_vendor=$(cat "$card/device/vendor" 2>/dev/null || true)
    nvidia_device=$(cat "$card/device/device" 2>/dev/null || true)
    break
done

[[ -n "$nvidia_card" && "$nvidia_pci" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]] || \
    fail 'could not identify the passed-through NVIDIA DRM card by its sysfs driver/PCI parent'
[[ "$nvidia_vendor" == 0x10de && "$nvidia_device" =~ ^0x[0-9a-fA-F]{4}$ ]] || \
    fail "unexpected NVIDIA PCI identifiers: vendor=$nvidia_vendor device=$nvidia_device"

if [[ "$MODE" == drm ]]; then
    hermes_card=''
    for card in /sys/class/drm/card[0-9]*; do
        name=${card##*/}
        [[ "$name" =~ ^card[0-9]+$ ]] || continue
        [[ -e "$card/device/driver" ]] || continue
        driver=$(readlink -f "$card/device/driver" 2>/dev/null || true)
        case "${driver##*/}" in
            hermes-kms|hermes_kms) hermes_card=$name; break ;;
        esac
    done
    [[ -n "$hermes_card" ]] || fail 'Hermes-KMS DRM card is not loaded; install/build the backend before configuring the compositor'
    rule_nvidia='SUBSYSTEM=="drm", ENV{DEVTYPE}=="drm_minor", ENV{DEVNAME}=="/dev/dri/card[0-9]*", SUBSYSTEMS=="pci", ATTRS{vendor}=="'+"$nvidia_vendor"+'", ATTRS{device}=="'+"$nvidia_device"+'", SYMLINK+="dri/idd-nvidia-card", TAG+="mutter-device-preferred-primary"'
    rule_hermes='SUBSYSTEM=="drm", ENV{DEVTYPE}=="drm_minor", ENV{DEVNAME}=="/dev/dri/card[0-9]*", DRIVERS=="hermes-kms", SYMLINK+="dri/idd-virtual-card"'
    printf '%s\n%s\n' "$rule_nvidia" "$rule_hermes" > "$RULE"
    install -d -m0755 "$ENV_DIR"
    cat > "$ENV_FILE" <<'EOF'
# Managed by Idd. Loaded by systemd user sessions at login.
# Keep the NVIDIA render GPU first; Hermes-KMS is the display-only output card.
KWIN_DRM_DEVICES=/dev/dri/idd-nvidia-card:/dev/dri/idd-virtual-card
EOF
else
    rule_nvidia='SUBSYSTEM=="drm", ENV{DEVTYPE}=="drm_minor", ENV{DEVNAME}=="/dev/dri/card[0-9]*", SUBSYSTEMS=="pci", ATTRS{vendor}=="'+"$nvidia_vendor"+'", ATTRS{device}=="'+"$nvidia_device"+'", SYMLINK+="dri/idd-nvidia-card", TAG+="mutter-device-preferred-primary"'
    printf '%s\n' "$rule_nvidia" > "$RULE"
    install -d -m0755 "$ENV_DIR"
    cat > "$ENV_FILE" <<'EOF'
# Managed by Idd. Loaded by user sessions at login.
# Restrict Aquamarine/Hyprland to the passed-through NVIDIA DRM card.
AQ_DRM_DEVICES=/dev/dri/idd-nvidia-card
EOF
fi

if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=drm --action=add
    udevadm settle --timeout=10 || true
fi

[[ -e /dev/dri/idd-nvidia-card ]] || fail 'udev did not create /dev/dri/idd-nvidia-card; inspect 61-idd-gpu-priority.rules and udevadm info'
if [[ "$MODE" == drm ]]; then
    [[ -e /dev/dri/idd-virtual-card ]] || fail 'udev did not create /dev/dri/idd-virtual-card; inspect the Hermes-KMS driver name with readlink -f /sys/class/drm/'"$hermes_card"'/device/driver'
    printf 'KWin primary GPU: /dev/dri/idd-nvidia-card (%s, PCI %s)\n' "$nvidia_card" "$nvidia_pci"
    printf 'KWin virtual output card: /dev/dri/idd-virtual-card (%s)\n' "$hermes_card"
else
    printf 'Hyprland GPU: /dev/dri/idd-nvidia-card (%s, PCI %s)\n' "$nvidia_card" "$nvidia_pci"
fi
printf 'Wrote %s. Log out and back in (or reboot) so the compositor receives the environment.\n' "$ENV_FILE"

#!/usr/bin/env bash
# Read-only diagnostics for a Linux VM with a passed-through NVIDIA GPU.
# Run inside the guest. This script makes no configuration changes.
set -u

section() { printf '\n========== %s ==========\n' "$1"; }

section "GUEST"
printf 'Kernel: '; uname -r
printf 'Virtualization: '
if command -v systemd-detect-virt >/dev/null 2>&1; then
  systemd-detect-virt || true
else
  printf 'systemd-detect-virt unavailable\n'
fi

section "NVIDIA"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version,pci.bus_id --format=csv,noheader || true
else
  echo "nvidia-smi not found"
fi
if command -v modinfo >/dev/null 2>&1; then
  for module in nvidia nvidia_drm nvidia_modeset; do
    if modinfo "$module" >/dev/null 2>&1; then
      printf '%s version=' "$module"
      modinfo -F version "$module" 2>/dev/null || true
      if [ "$module" = nvidia ]; then
        printf '  license='
        modinfo -F license nvidia 2>/dev/null || true
      fi
    else
      printf '%s: not found\n' "$module"
    fi
  done
fi
if [ -r /sys/module/nvidia_drm/parameters/modeset ]; then
  printf 'nvidia_drm modeset='
  cat /sys/module/nvidia_drm/parameters/modeset
else
  echo "nvidia_drm modeset parameter unavailable (module may not be loaded)"
fi

section "PCI GPU BINDINGS"
if command -v lspci >/dev/null 2>&1; then
  lspci -nnk | grep -A4 -Ei 'VGA compatible controller|3D controller|Audio device' || true
else
  echo "lspci not found"
fi

section "DRM CARDS AND CONNECTORS"
shopt -s nullglob
for card_path in /sys/class/drm/card[0-9]*; do
  card=$(basename "$card_path")
  [[ "$card" =~ ^card[0-9]+$ ]] || continue
  [ -e "$card_path/device" ] || continue
  device=$(readlink -f "$card_path/device" 2>/dev/null || echo unknown)
  driver=$(readlink -f "$card_path/device/driver" 2>/dev/null || true)
  driver=${driver##*/}
  printf '\n%s device=%s driver=%s\n' "$card" "$device" "${driver:-unknown}"
  for status_path in "$card_path"-*/status; do
    [ -r "$status_path" ] || continue
    connector_dir=${status_path%/status}
    connector=$(basename "$connector_dir")
    status=$(cat "$status_path" 2>/dev/null || echo unknown)
    edid_bytes=0
    if [ -r "$connector_dir/edid" ]; then
      edid_bytes=$(wc -c < "$connector_dir/edid")
    fi
    printf '  %-22s status=%-12s edid_bytes=%s\n' "$connector" "$status" "$edid_bytes"
  done
done

section "DRM RENDER NODES"
render_nodes=(/sys/class/drm/renderD*)
if (( ${#render_nodes[@]} == 0 )); then
  echo "No DRM render nodes exposed."
else
  for render_path in "${render_nodes[@]}"; do
    render=$(basename "$render_path")
    device=$(readlink -f "$render_path/device" 2>/dev/null || echo unknown)
    driver=$(readlink -f "$render_path/device/driver" 2>/dev/null || true)
    driver=${driver##*/}
    printf '%-12s device=%s driver=%s\n' "$render" "$device" "${driver:-unknown}"
    if [[ -r "$render_path/device/vendor" && -r "$render_path/device/device" ]]; then
      printf '  PCI vendor=%s device=%s\n' \
        "$(cat "$render_path/device/vendor")" "$(cat "$render_path/device/device")"
    fi
  done
fi

section "DRM BY-PATH LINKS"
if [[ -d /dev/dri/by-path ]]; then
  find /dev/dri/by-path -maxdepth 1 -type l -printf '%f -> %l\n' | sort
else
  echo "/dev/dri/by-path is unavailable."
fi

section "BOOT PARAMETERS"
cat /proc/cmdline

section "NOTES"
echo "This report describes the guest only. It cannot verify host-side VFIO binding."
echo "Run the equivalent read-only checks on the hypervisor separately if host isolation needs auditing."

#!/usr/bin/env bash
# Run only inside the Linux guest. Installation does not enable the output.
set -euo pipefail
guest_only=0
destdir=
while (($#)); do
  case "$1" in
    --guest-only) guest_only=1; shift ;;
    --destdir) destdir=${2:?Missing staging directory}; shift 2 ;;
    *) printf 'Usage: %s --guest-only [--destdir STAGING_DIRECTORY]\n' "$0" >&2; exit 2 ;;
  esac
done
if ((guest_only != 1)); then
  echo 'Run inside the VM and pass --guest-only. Do not install on the hypervisor.' >&2
  exit 2
fi
if [[ -z "$destdir" && $EUID != 0 ]]; then
  echo 'Installation requires root inside the Linux guest.' >&2
  exit 1
fi
if [[ -n "$destdir" && "$destdir" != /* ]]; then
  echo '--destdir must be an absolute staging path.' >&2
  exit 2
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install -Dm755 "$source_dir/src/idd.py" "$destdir/usr/local/bin/idd"
install -Dm644 "$source_dir/systemd/idd-display.service" "$destdir/etc/systemd/system/idd-display.service"
install -Dm644 "$source_dir/udev/71-idd-hotplug.rules" "$destdir/etc/udev/rules.d/71-idd-hotplug.rules"
if [[ ! -e "$destdir/etc/idd/modes.json" ]]; then
  install -Dm644 "$source_dir/config/modes.json" "$destdir/etc/idd/modes.json"
fi
if [[ -z "$destdir" ]]; then
  systemctl daemon-reload
  udevadm control --reload-rules
fi
if [[ -f "$destdir/etc/idd/config.json" ]]; then
  echo 'Updated. Your GPU/port selection and existing modes.json were preserved.'
  echo 'Apply the mode profile now: sudo idd apply'
else
  echo 'Installed. Next: idd list, idd configure --guest-only --pci ... --connector ...'
  echo 'Then: systemctl enable --now idd-display.service'
fi

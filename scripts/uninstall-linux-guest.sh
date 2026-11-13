#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run as root inside the guest.' >&2; exit 1; }
[[ ${1:-} == --guest ]] || { echo 'Refusing: pass --guest and run ONLY inside the VM.' >&2; exit 1; }

virt="$(systemd-detect-virt --vm 2>/dev/null || true)"
[[ "$virt" == "none" ]] && virt=""
dmi="$( { cat /sys/class/dmi/id/sys_vendor 2>/dev/null; cat /sys/class/dmi/id/product_name 2>/dev/null; } | tr '[:upper:]' '[:lower:]' )"
if [[ -z "$virt" && ! "$dmi" =~ (qemu|kvm|vmware|virtualbox|xen|bochs|virtual) ]]; then
    if ! command -v lspci >/dev/null 2>&1 || ! lspci -nnk 2>/dev/null | grep -qiE 'QEMU Virtual Machine|VMware|VirtualBox|KVM|Xen|Bochs'; then
        echo 'No hypervisor marker detected; refusing to uninstall on this OS.' >&2
        exit 1
    fi
fi

[[ -r /etc/idd/backend ]] || {
    echo 'No /etc/idd/backend marker found. Refusing to remove a possibly unrelated Hermes-KMS installation.' >&2
    exit 1
}
backend=$(</etc/idd/backend)
case "$backend" in
    drm|hyprland) ;;
    *) echo "Unknown Idd backend marker '$backend'; refusing removal." >&2; exit 1 ;;
esac

kms_version=''
if [[ "$backend" == drm ]]; then
    [[ -r /etc/idd/hermes-kms-version ]] || {
        echo 'Missing /etc/idd/hermes-kms-version; refusing to remove an unowned DKMS version.' >&2
        exit 1
    }
    kms_version=$(</etc/idd/hermes-kms-version)
    [[ "$kms_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
        echo "Invalid Hermes-KMS version marker '$kms_version'; refusing removal." >&2
        exit 1
    }
fi

printf 'This removes the Idd %s integration from this guest.\n' "$backend"
printf 'Type REMOVE-FROM-GUEST to continue: '
read -r answer
[[ "$answer" == REMOVE-FROM-GUEST ]] || { echo 'Cancelled.'; exit 1; }

if [[ "$backend" == drm ]]; then
    systemctl disable --now idd-virtual-display.service 2>/dev/null || true
    rm -f /etc/systemd/system/idd-virtual-display.service
    systemctl daemon-reload
    if [[ -d /sys/module/hermes_kms ]] && ! modprobe -r hermes_kms 2>/dev/null; then
        # A running compositor or other DRM client still has the module open.
        # Disable boot-time loading, but keep DKMS source and module files so a
        # second uninstall after reboot can cleanly unregister them.
        rm -f /usr/lib/modules-load.d/hermes-kms.conf
        rm -f /usr/lib/modprobe.d/hermes-kms.conf
        echo 'Hermes-KMS is still in use. Boot autoload has been disabled; reboot this guest and rerun the same uninstaller command. No DKMS/module source files were removed.' >&2
        exit 1
    fi
    if command -v dkms >/dev/null 2>&1; then
        # Remove only the version recorded by this Idd install. Do not remove
        # a later independently installed Hermes-KMS version.
        dkms remove -m hermes-kms -v "$kms_version" --all 2>/dev/null || true
    fi
    rm -rf -- "/usr/src/hermes-kms-$kms_version" 2>/dev/null || true
    rm -f /usr/lib/modules-load.d/hermes-kms.conf /usr/lib/modprobe.d/hermes-kms.conf
    rm -f /usr/lib/udev/rules.d/72-hermes-kms-render-access.rules /usr/lib/udev/rules.d/92-hermes-kms-access.rules /usr/lib/udev/rules.d/95-hermes-kms-host-access.rules
    rm -f /usr/lib/hermes-kms/hermes-kms-render-acl /usr/bin/hermes-kms-unload /usr/bin/hermes-kms-rebind
    rm -f /usr/include/drm/hermes_kms_drm.h /usr/include/hermes-kms/hermes_session.h /usr/share/licenses/hermes-kms/MIT.txt
    rmdir /usr/include/hermes-kms /usr/share/licenses/hermes-kms 2>/dev/null || true
fi

rm -f /usr/local/bin/iddctl /usr/local/bin/idd-kmsctl /usr/local/bin/idd-configure-compositor-gpus
rm -f /etc/udev/rules.d/61-idd-gpu-priority.rules /etc/environment.d/90-idd-virtual-display.conf
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=drm --action=change || true
fi
rm -f /etc/idd/backend /etc/idd/hermes-kms-version
printf 'Removed guest-side Idd %s integration.\n' "$backend"
if [[ "$backend" == drm ]]; then
    printf 'Reboot the guest to ensure the module is fully unloaded.\n'
fi
printf 'The configuration file /etc/idd/virtual-display.conf was preserved.\n'

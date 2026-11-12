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

printf 'This removes the guest-local IDD service and Hermes-KMS DKMS module from this OS.\n'
printf 'Type REMOVE-FROM-GUEST to continue: '
read -r answer
[[ "$answer" == REMOVE-FROM-GUEST ]] || { echo 'Cancelled.'; exit 1; }

systemctl disable --now idd-virtual-display.service 2>/dev/null || true
rm -f /etc/systemd/system/idd-virtual-display.service
systemctl daemon-reload
if command -v modprobe >/dev/null 2>&1; then
    modprobe -r hermes_kms 2>/dev/null || echo 'hermes_kms is still in use; reboot the guest before removing the remaining module files.' >&2
fi
rm -f /usr/local/bin/iddctl /usr/local/bin/idd-kmsctl

if command -v dkms >/dev/null 2>&1; then
    dkms status | awk -F'[/,:]' '$1 == "hermes-kms" && $2 != "" {print $2}' | sort -u | while read -r version; do
        [[ -n "$version" ]] && dkms remove -m hermes-kms -v "$version" --all || true
    done
fi
rm -rf /usr/src/hermes-kms-* 2>/dev/null || true
rm -f /usr/lib/modules-load.d/hermes-kms.conf /usr/lib/modprobe.d/hermes-kms.conf
rm -f /usr/lib/udev/rules.d/72-hermes-kms-render-access.rules /usr/lib/udev/rules.d/92-hermes-kms-access.rules /usr/lib/udev/rules.d/95-hermes-kms-host-access.rules
rm -f /usr/lib/hermes-kms/hermes-kms-render-acl /usr/bin/hermes-kms-unload /usr/bin/hermes-kms-rebind
rm -f /usr/include/drm/hermes_kms_drm.h /usr/include/hermes-kms/hermes_session.h /usr/share/licenses/hermes-kms/MIT.txt
rmdir /usr/include/hermes-kms /usr/share/licenses/hermes-kms 2>/dev/null || true
printf 'Removed guest-side IDD integration. Reboot the guest to ensure the module is fully unloaded.\n'
printf 'The configuration directory /etc/idd was preserved.\n'

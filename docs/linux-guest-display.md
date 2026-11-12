# Linux guest virtual display (NVIDIA passthrough)

This project installs a **guest-local** virtual DRM/KMS display. The host must not run any IDD component and the host's NVIDIA connectors are never force-enabled or assigned EDIDs.

## Your reported machine

The supplied diagnostics show:

- NVIDIA GeForce RTX 3080 Ti, driver 615.78.08;
- NVIDIA kernel module license `Dual MIT/GPL` (the NVIDIA open kernel-module flavor);
- `nvidia_drm` KMS modesetting enabled (`Y`);
- NVIDIA device at guest PCI address `03:00.0`;
- a guest DRM card driven by `nvidia` with three disconnected DP connectors and one connected HDMI connector.

The install does **not** alter any of those NVIDIA connector states. It loads a separate virtual DRM device called Hermes-KMS, then creates its own `HERMES-1` output. The installer configures the RTX 3080 Ti to be the compositor's primary rendering GPU; Hermes-KMS is only the display endpoint. KWin receives `KWIN_DRM_DEVICES` with the NVIDIA card first and Hermes-KMS second. It also receives `KWIN_RENDER_NODES=/dev/dri/idd-nvidia-render` on KWin 6.7+, which restricts KWin's direct render-node opens to the NVIDIA render node. Mutter receives its documented `mutter-device-preferred-primary` udev tag for the RTX 3080 Ti PCI ID. Whether the NVIDIA stack successfully renders and presents to the virtual card still requires hardware validation. `iddctl verify` and `nvidia-smi` confirm the actual renderer after a fresh login.

## Isolation guarantee and safety boundary

The installer must be executed inside the VM. It checks for common hypervisor markers, requires `--guest`, and then asks for the exact confirmation `INSTALL-IN-GUEST`. It modifies only the kernel and filesystem of the OS where it runs. PCI passthrough is what makes that OS the guest: the host should bind the passed-through GPU functions to `vfio-pci` and must not load NVIDIA for that GPU.

There is no host-side command, systemd unit, initramfs hook, EDID override, NVIDIA connector override, or host VKMS/EVDI installation in this project. The generic `drm.edid_firmware` route is deliberately avoided: NVIDIA 615.78.08 has a reported leak when EDID overrides are probed on disconnected connectors ([NVIDIA forum report, October 26, 2026](https://forums.developer.nvidia.com/t/615-every-screen-lock-now-sends-hdcp-hotplug-uevents-which-trigger-an-nvidia-drm-edid-override-leak-on-disconnected-connectors-kmalloc-128/386981)). When the VM is stopped, the guest's DRM card and connector are destroyed with the guest. When the guest's service is stopped, the Hermes-KMS owner fd closes and its connector is hot-unplugged.

**Do not run the installer or uninstaller on the hypervisor.** No script can cryptographically prove from inside a guest that a device is exclusively VFIO-assigned; host binding is a hypervisor responsibility.

### Host-side isolation check (hypervisor only)

The guest BDF in the diagnostic output (`0000:03:00.0`) is a guest PCI address; do not assume it matches the host's BDF. On the hypervisor, find the passed-through GPU and its audio/USB functions with `lspci -nnk`. Each function assigned to the VM must be bound to `vfio-pci`, not `nvidia` or `nouveau`. Check the host's `/sys/class/drm` before the VM starts and again after it is shut down: this project must not add any host DRM card or connector. There is intentionally no host installer, systemd service, module, EDID file, initramfs hook, or display-manager configuration.

## Backend and compositor support

The project pins upstream Hermes-KMS `v0.4.1` rather than copying a second DRM driver implementation into this repository. Hermes-KMS is an out-of-tree GPL-2.0 DRM/KMS driver with a synthetic EDID, a DRM connector and mode setting. The virtual card is not itself a render GPU. This repository supplies the guest-only installer, lifetime service, stable guest udev aliases, and compositor GPU-selection configuration: NVIDIA is primary for KWin/Mutter; Hermes-KMS is the secondary output-only card.

- **KDE Plasma / KWin Wayland:** the installer writes `KWIN_DRM_DEVICES=/dev/dri/idd-nvidia-card:/dev/dri/idd-virtual-card`, so the RTX 3080 Ti is first (render GPU) and Hermes-KMS is second (virtual output). KWin reads this when the session starts; log out/in after installation. The output is activated by KScreen; run `iddctl activate-kde` or enable it in System Settings → Display Configuration.
- **GNOME Wayland:** the installer tags the RTX 3080 Ti PCI device with Mutter's documented `mutter-device-preferred-primary` udev tag, making it the primary renderer when it supports EGL. The Hermes-KMS card remains the virtual output. Log out/in after installation, then use Settings → Displays; `iddctl activate-gnome` opens that panel.
- **Hyprland:** Hermes-KMS/EVDI are not currently a working KMS-output path for Aquamarine. The default installer also writes `AQ_NO_KMS_REQUIREMENT=1` and `AQ_DRM_DEVICES=/dev/dri/idd-nvidia-card`; `iddctl hyprland-start` then creates a Hyprland-native `headless` output and sets its mode through `wlr-randr`. It is intended to render through the passed-through RTX 3080 Ti. This is a compositor virtual output, not a `/sys/class/drm` connector. Remove it with `iddctl hyprland-stop`. You can therefore use one default installation for KDE, GNOME and Hyprland without reinstalling when changing desktop sessions. Hyprland documents `hyprctl output create headless` and `output remove`; the output-management protocol applies its mode.

## Install (inside the guest only)

### CachyOS / Arch

1. Boot the VM with the RTX 3080 Ti passed through and install the NVIDIA driver **inside the guest**.
2. Confirm `nvidia-smi` works and `cat /sys/module/nvidia_drm/parameters/modeset` prints `Y`.
3. The installer detects the running kernel's Arch package base via `/usr/lib/modules/$(uname -r)/pkgbase` and installs the corresponding `-headers` package. On your `7.3.1-1-cachyos` kernel this should resolve to `linux-cachyos-headers`. It verifies both that `/lib/modules/$(uname -r)/build/Makefile` exists and that `make -s -C /lib/modules/$(uname -r)/build kernelrelease` exactly matches `uname -r`. If pacman updates the kernel while installing headers, reboot into that kernel and rerun the installer.
4. Clone the implementation branch in the guest and run:

   ```sh
   git clone --branch linux-guest-virtual-display-prototype --single-branch https://github.com/oltrar/Idd.git
   cd Idd
   sudo bash ./scripts/install-linux-guest.sh --guest
   ```

5. Read the safety prompt and type `INSTALL-IN-GUEST` only after verifying this is the VM.
6. The default `--compositor=drm` installer installs `base-devel`, `dkms`, `git`, `pahole`, and `wlr-randr`, builds Hermes-KMS against the exact running kernel, builds the control utility, installs a guest systemd service, and starts the virtual DRM output at 1920x1080@60 by default.

### Debian / Ubuntu

Install the NVIDIA driver in the guest and use the same installer command. The script installs `build-essential`, `dkms`, `git`, `dwarves` (pahole), `wlr-randr`, and the exact `linux-headers-$(uname -r)` package (or `custom-kernel-headers-$(uname -r)` when provided by a custom kernel vendor). It checks the header tree's `kernelrelease` equals the running kernel before starting DKMS.

If matching headers are not available, the DRM installer stops before compiling. Install matching headers or boot a kernel whose headers are available, then rerun.

### Hyprland-only install

The default installation configures NVIDIA rendering for Hyprland's headless output as well as installing the KDE/GNOME DRM endpoint. Use this reduced installation only for a Hyprland-only VM where you explicitly do not need the KDE/GNOME DRM connector. Hyprland/Aquamarine does not currently use Hermes-KMS as a reliable secondary DRM output.

```sh
sudo bash ./scripts/install-linux-guest.sh --guest --compositor=hyprland
```

This optional mode installs `iddctl` and `wlr-randr` but does not build/load Hermes-KMS or install the systemd DRM-output service. It creates the output inside the logged-in Hyprland session. The installer creates stable guest symlinks `/dev/dri/idd-nvidia-card` and, when available, `/dev/dri/idd-nvidia-render`. It writes `AQ_NO_KMS_REQUIREMENT=1` and `AQ_DRM_DEVICES=/dev/dri/idd-nvidia-card` so Aquamarine can use the passed-through RTX 3080 Ti for a headless session even when no NVIDIA connector is physically connected. Log out/in after installation. This remains a compositor headless output, not a guest DRM connector.

## Use

The driver service starts at guest boot:

```sh
iddctl status
iddctl verify
```

The default mode is 1920x1080@60. For the KDE/GNOME DRM backend, change `/etc/idd/virtual-display.conf` to another accepted `IDD_MODE=WIDTHxHEIGHT@HZ`, then run `sudo systemctl restart idd-virtual-display.service`. For Hyprland, change the same setting and rerun `iddctl hyprland-start`.

### KDE Plasma Wayland

```sh
iddctl activate-kde
```

Or open System Settings → Display Configuration and enable `HERMES-1`. This command only enables the virtual output; it does not disable or move your physical guest monitor.

### GNOME Wayland

```sh
iddctl activate-gnome
```

Enable `HERMES-1` in Settings → Displays. Existing physical displays are not disabled by this tool.

### Hyprland

Use the default DRM installation for a multi-desktop VM, or the optional `--compositor=hyprland` installation mode for a Hyprland-only VM. Run these commands as the logged-in Hyprland user, **without sudo**:

```sh
iddctl hyprland-start
iddctl hyprland-stop
```

This creates/removes the named Hyprland headless output. The output disappears when removed or when the Hyprland session ends. It does not create a guest DRM connector. With the default DRM installation, Hermes-KMS remains available for KDE/GNOME sessions but Hyprland is configured to use the NVIDIA card and its own headless output. The Hyprland-only installer deliberately does not install Hermes-KMS or the systemd DRM-output service.

## Verify the guest's display and renderer

For the KDE/GNOME DRM backend:

```sh
iddctl verify
ls -l /dev/dri/by-path/ | grep -i hermes
ls /sys/class/drm/ | grep HERMES
nvidia-smi
```

Expected: a separate `hermes-kms` DRM card/connector appears in the guest alongside the NVIDIA DRM card. Do not expect `HERMES-1` to be listed under the NVIDIA card's `DP-*` or `HDMI-*` connectors. The separation is intentional. Check `/dev/dri/idd-nvidia-render` exists; on KWin 6.7+, check the session environment contains `KWIN_RENDER_NODES=/dev/dri/idd-nvidia-render`. KWin's first `KWIN_DRM_DEVICES` entry is its primary DRM GPU.

For the Hyprland-only backend, verify the compositor output instead:

```sh
hyprctl monitors all
nvidia-smi
```

The compositor still has to successfully open the selected renderer. Verify NVIDIA utilization and compositor processes with `nvidia-smi` while the desktop is drawing; also inspect `journalctl --user -b` for KWin/Mutter renderer initialization errors. NVIDIA's open kernel-module flavor is indicated by the diagnostic's `Dual MIT/GPL` license; the userspace driver stack remains NVIDIA's driver. The installer writes only the dedicated Idd environment file and udev rules inside the guest.

## Stop / uninstall

Stop the virtual display (the connector is disconnected when the owner process exits):

```sh
sudo iddctl stop
```

Uninstall the guest integration and DKMS module:

```sh
sudo bash ./scripts/uninstall-linux-guest.sh --guest
```

The uninstaller has its own VM-marker check, project backend marker and confirmation. It only removes Hermes-KMS when `/etc/idd/backend` says this Idd installation installed the DRM backend; a Hyprland-only install will not remove an unrelated Hermes-KMS module. If a compositor still holds the DRM module open, the uninstaller disables boot autoload and exits without deleting DKMS/module sources; reboot the guest and rerun the same uninstall command. It preserves `/etc/idd/virtual-display.conf`.

## Test results and limitations

The user-provided RTX 3080 Ti diagnostics are documented above. The installer now installs matching CachyOS/Arch headers before DKMS and refuses to take over an independently installed Hermes-KMS module. This repository cannot run kernel builds or Wayland compositor tests against that remote VM from CI, so the actual RTX 3080 Ti + NVIDIA 615.78.08 + CachyOS 7.3.1-1 installation still needs to be validated by running the installer in that guest. A successful DKMS build proves the kernel module compiled, not that every compositor activates the monitor correctly.

Hyprland has a distinct headless backend, not a DRM connector. Use `--compositor=hyprland` for that mode; it deliberately does not load Hermes-KMS. The KDE/GNOME path is the true guest DRM/KMS virtual monitor path.

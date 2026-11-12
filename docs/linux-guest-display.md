# Linux guest virtual display (NVIDIA passthrough)

This project installs a **guest-local** virtual DRM/KMS display. The host must not run any IDD component and the host's NVIDIA connectors are never force-enabled or assigned EDIDs.

## Your reported machine

The supplied diagnostics show:

- NVIDIA GeForce RTX 3080 Ti, driver 615.78.08;
- NVIDIA kernel module license `Dual MIT/GPL` (the NVIDIA open kernel-module flavor);
- `nvidia_drm` KMS modesetting enabled (`Y`);
- NVIDIA device at guest PCI address `03:00.0`;
- a guest DRM card driven by `nvidia` with three disconnected DP connectors and one connected HDMI connector.

The install does **not** alter any of those NVIDIA connector states. It loads a separate virtual DRM device called Hermes-KMS, then creates its own `HERMES-1` output. The RTX 3080 Ti is the rendering GPU; Hermes-KMS is only the display endpoint. The installer now configures the supported compositor selection mechanisms: KWin receives `KWIN_DRM_DEVICES` with the NVIDIA card first and Hermes-KMS second, and Mutter receives its documented `mutter-device-preferred-primary` udev tag for the RTX 3080 Ti PCI ID. This is still subject to hardware validation on your driver/kernel. `iddctl verify` and `nvidia-smi` are used to confirm the actual renderer after a fresh login.

## Isolation guarantee and safety boundary

The installer must be executed inside the VM. It checks for common hypervisor markers, requires `--guest`, and then asks for the exact confirmation `INSTALL-IN-GUEST`. It modifies only the kernel and filesystem of the OS where it runs. PCI passthrough is what makes that OS the guest: the host should bind the passed-through GPU functions to `vfio-pci` and must not load NVIDIA for that GPU.

There is no host-side command, systemd unit, initramfs hook, EDID override, NVIDIA connector override, or host VKMS/EVDI installation in this project. When the VM is stopped, the guest's DRM card and connector are destroyed with the guest. When the guest's service is stopped, the Hermes-KMS owner fd closes and its connector is hot-unplugged.

**Do not run the installer or uninstaller on the hypervisor.** No script can cryptographically prove from inside a guest that a device is exclusively VFIO-assigned; host binding is a hypervisor responsibility.

## Backend and compositor support

The project pins upstream Hermes-KMS `v0.4.0` rather than copying a second DRM driver implementation into this repository. Hermes-KMS is an out-of-tree GPL-2.0 DRM/KMS driver with a synthetic EDID, a DRM connector and mode setting. The virtual card is not itself a render GPU. This repository supplies the guest-only installer, lifetime service, stable guest udev aliases, and compositor GPU-selection configuration: NVIDIA is primary for KWin/Mutter; Hermes-KMS is the secondary output-only card.

- **KDE Plasma / KWin Wayland:** the installer writes `KWIN_DRM_DEVICES=/dev/dri/idd-nvidia-card:/dev/dri/idd-virtual-card`, so the RTX 3080 Ti is first (render GPU) and Hermes-KMS is second (virtual output). KWin reads this when the session starts; log out/in after installation. The output is activated by KScreen; run `iddctl activate-kde` or enable it in System Settings → Display Configuration.
- **GNOME Wayland:** the installer tags the RTX 3080 Ti PCI device with Mutter's documented `mutter-device-preferred-primary` udev tag, making it the primary renderer when it supports EGL. The Hermes-KMS card remains the virtual output. Log out/in after installation, then use Settings → Displays; `iddctl activate-gnome` opens that panel.
- **Hyprland:** Hermes-KMS/EVDI are not currently a working KMS-output path for Aquamarine. `iddctl hyprland-start` creates a Hyprland-native `headless` output and sets its mode through `wlr-randr`. It renders through the GPU selected by Hyprland. This is a compositor virtual output, not a `/sys/class/drm` connector. Remove it with `iddctl hyprland-stop`. Hyprland documents `hyprctl output create headless` and `output remove`; the output-management protocol applies its mode.

## Install (inside the guest only)

### CachyOS / Arch

1. Boot the VM with the RTX 3080 Ti passed through and install the NVIDIA driver **inside the guest**.
2. Confirm `nvidia-smi` works and `cat /sys/module/nvidia_drm/parameters/modeset` prints `Y`.
3. Ensure headers matching the running kernel are installed. On CachyOS this is normally `linux-cachyos-headers`; on Arch use the headers package matching the installed kernel.
4. Clone this repository in the guest and run:

   ```sh
   sudo bash ./scripts/install-linux-guest.sh --guest
   ```

5. Read the safety prompt and type `INSTALL-IN-GUEST` only after verifying this is the VM.
6. The default `--compositor=drm` installer installs `base-devel`, `dkms`, `git`, and `wlr-randr`, builds Hermes-KMS against the exact running kernel, builds the control utility, installs a guest systemd service, and starts the virtual DRM output at 1920x1080@60 by default.

### Debian / Ubuntu

Install a kernel with matching headers available through APT, install the NVIDIA driver in the guest, and use the same installer command. The script installs `build-essential`, `dkms`, `git`, `wlr-randr`, and `linux-headers-$(uname -r)`.

If matching headers are not available, the DRM installer stops before compiling. Install matching headers or boot a kernel whose headers are available, then rerun.

### Hyprland-only install\n\nHyprland/Aquamarine does not currently use Hermes-KMS as a reliable secondary DRM output. To avoid loading a display card that Hyprland cannot drive, install only the compositor-native headless backend instead:\n\n```sh\nsudo bash ./scripts/install-linux-guest.sh --guest --compositor=hyprland\n```\n\nThis mode installs `iddctl` and `wlr-randr` but does not build/load Hermes-KMS or install the systemd DRM-output service. It creates the output inside the logged-in Hyprland session. If another GPU is present and the RTX 3080 Ti is not already the primary renderer, set `AQ_DRM_DEVICES` in your Hyprland environment to the NVIDIA card. Your diagnostics currently identify `/dev/dri/card1` as NVIDIA; verify that path again after reboot because DRM card numbers can change.\n\n## Use

The driver service starts at guest boot:

```sh
iddctl status
iddctl verify
```

The default mode is 1920x1080@60. Change `/etc/idd/virtual-display.conf` to another accepted `IDD_MODE=WIDTHxHEIGHT@HZ`, then run `sudo systemctl restart idd-virtual-display.service`.

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

Use the `--compositor=hyprland` installation mode above. Run these commands as the logged-in Hyprland user, **without sudo**:

```sh
iddctl hyprland-start
iddctl hyprland-stop
```

This creates/removes the named Hyprland headless output. The output disappears when removed or when the Hyprland session ends. It does not create a guest DRM connector. The Hyprland-only installer deliberately does not install Hermes-KMS or the systemd DRM-output service.

## Verify the guest's display and renderer

For the KDE/GNOME DRM backend:

```sh
iddctl verify
ls -l /dev/dri/by-path/ | grep -i hermes
ls /sys/class/drm/ | grep HERMES
nvidia-smi
```

Expected: a separate `hermes-kms` DRM card/connector appears in the guest alongside the NVIDIA DRM card. Do not expect `HERMES-1` to be listed under the NVIDIA card's `DP-*` or `HDMI-*` connectors. The separation is intentional.

For the Hyprland-only backend, verify the compositor output instead:

```sh
hyprctl monitors all
nvidia-smi
```

The compositor still has to successfully open the selected renderer. Verify NVIDIA utilization and compositor processes with `nvidia-smi` while the desktop is drawing; also inspect `journalctl --user -b` for KWin/Mutter renderer initialization errors. The installer writes only the dedicated Idd environment file and udev rules inside the guest.

## Stop / uninstall

Stop the virtual display (the connector is disconnected when the owner process exits):

```sh
sudo iddctl stop
```

Uninstall the guest integration and DKMS module:

```sh
sudo bash ./scripts/uninstall-linux-guest.sh --guest
```

The uninstaller has its own VM-marker check and confirmation. It preserves `/etc/idd/virtual-display.conf`; reboot the guest after removal to ensure the module is unloaded.

## Test results and limitations

The user-provided RTX 3080 Ti diagnostics are documented above. This repository cannot run kernel builds or Wayland compositor tests against that remote VM from CI, so the actual RTX 3080 Ti + NVIDIA 615.78.08 + CachyOS 7.3.1-1 installation remains to be validated by running the installer in that guest. A successful DKMS build proves the kernel module compiled, not that every compositor activates the monitor correctly.

Hyprland has a distinct headless backend, not a DRM connector. Use `--compositor=hyprland` for that mode; it deliberately does not load Hermes-KMS. The KDE/GNOME path is the true guest DRM/KMS virtual monitor path.

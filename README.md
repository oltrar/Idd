# Idd

Linux guest-only virtual display experiments inspired by Windows' Indirect Display Driver (IDD).

## Status

**Experimental proof of concept — not an NVIDIA DRM connector driver.**

The Linux implementation in this branch uses [EVDI](https://github.com/DisplayLink/evdi) to create a real DRM/KMS virtual display device from inside the guest. The display is visible to Wayland compositors that support EVDI outputs, but it is a separate DRM device; it is **not** a connector on the NVIDIA DRM device.

The NVIDIA proprietary Linux driver does not expose a supported public interface for third-party code to add a virtual connector to its own DRM device. Do not use `drm.edid_firmware`, `video=...`, or an Xorg `ConnectedMonitor` setting as if they created a new NVIDIA KMS connector: those mechanisms do not turn an arbitrary disconnected NVIDIA connector into a supported, hot-pluggable virtual KMS output on Wayland.

This prototype is deliberately honest about that limitation. It provides a guest-local lifecycle helper and setup notes so the virtual display is created only when requested.

## Isolation requirement

Install and run this project **inside the VM only**.

With PCI passthrough, the guest owns the passed-through GPU while the VM is running. The EVDI module and virtual display created by this project are also guest kernel objects. Nothing in this project writes host display configuration, changes host DRM connectors, or asks for a physical dummy plug.

When the VM is shut down, its guest kernel and EVDI device cease to exist. The host does not acquire a virtual monitor from this project. This assumes the GPU is correctly assigned to the VM and is not simultaneously bound to a host graphics driver.

## What this prototype provides

- Creates and removes an EVDI DRM display on demand inside the guest.
- Does not load EVDI automatically at host boot (there is no host component).
- Checks that the NVIDIA driver is present and reports DRM devices for diagnostics.
- Works as a starting point for testing KDE Plasma, GNOME, and wlroots/Hyprland compositor behaviour.

**GPU rendering note:** EVDI is a separate virtual display device. Whether the compositor renders the desktop with the passed-through NVIDIA GPU depends on compositor/device selection. Verify with `nvidia-smi` while rendering. This prototype does not claim that EVDI's virtual connector belongs to NVIDIA or that every compositor will choose NVIDIA automatically.

## Requirements

- Linux guest with the passed-through NVIDIA GPU and the proprietary NVIDIA driver installed.
- NVIDIA DRM KMS enabled (`nvidia_drm.modeset=1`; verify with `cat /sys/module/nvidia_drm/parameters/modeset`).
- EVDI kernel module built for the guest's running kernel.
- Root privileges to load/unload EVDI and create/remove virtual devices.

## Install EVDI

### Arch Linux

Install matching kernel headers and DKMS, then install an EVDI DKMS package from the AUR (for example, `evdi-dkms`) using your preferred AUR helper:

```bash
sudo pacman -S --needed dkms linux-headers
# Example with paru; review the PKGBUILD before installing:
paru -S evdi-dkms
```

If using a non-default kernel, install its matching headers instead of `linux-headers` (for example, `linux-lts-headers`).

### Debian

Install build dependencies and headers for the running kernel:

```bash
sudo apt update
sudo apt install dkms build-essential linux-headers-$(uname -r)
```

Install EVDI using a trusted Debian package/repository that provides `evdi-dkms`, or build/package the upstream [DisplayLink/evdi](https://github.com/DisplayLink/evdi) module for your exact kernel. Do not install an unsigned, prebuilt module intended for a different kernel.

After installation, verify:

```bash
modinfo evdi
```

## Use

Copy `scripts/idd-evdi` into `/usr/local/bin/idd-evdi`:

```bash
sudo install -m 0755 scripts/idd-evdi /usr/local/bin/idd-evdi
```

Create one virtual display:

```bash
sudo idd-evdi start
```

Inspect it:

```bash
idd-evdi status
ls -l /dev/dri
```

Stop and remove the virtual display:

```bash
sudo idd-evdi stop
```

The helper does not enable EVDI at boot. You decide when to start and stop it. The compositor may need to be restarted or asked to rescan outputs after creating the device, depending on the desktop environment and EVDI version.

## Verify NVIDIA rendering

1. Start the virtual display.
2. Open the compositor's display settings and enable the EVDI output.
3. Run `nvidia-smi` while rendering a desktop animation or GPU workload.
4. Inspect the compositor's logs and DRM device selection if rendering occurs on another GPU.

For Hyprland/wlroots, `WLR_DRM_DEVICES` can influence DRM device selection, but its exact value must be determined from the guest's `/dev/dri/by-path/` and the compositor's support. Do not copy a guessed card number. KDE Plasma and GNOME use their own device-selection logic.

## Important distinction from Windows IDD

Windows IddCx allows an indirect display driver to enumerate a monitor without claiming a physical connector. The Windows sample can select a D3D rendering adapter for its swap chain. Linux has no directly equivalent, stable, cross-compositor userspace IDD API. EVDI is a generic DRM platform device and is not discoverable as a child connector of NVIDIA; see the [EVDI project documentation](https://github.com/DisplayLink/evdi) and [Linux DRM/KMS documentation](https://docs.kernel.org/gpu/drm-kms.html).

A future implementation that meets the strict requirement — a virtual connector owned by the NVIDIA DRM device, with NVIDIA rendering and hotplug lifecycle entirely in the guest — would require support from NVIDIA's DRM driver or a supported NVIDIA integration point. This repository does not pretend that an out-of-tree module can safely attach arbitrary connectors to the proprietary NVIDIA driver.

## Uninstall

Remove the helper:

```bash
sudo rm -f /usr/local/bin/idd-evdi
```

Stop the display first, then remove EVDI using the same package manager/DKMS mechanism used to install it. The NVIDIA driver is not modified.

## Safety

This is an experimental helper, not a production display driver. It does not alter bootloader parameters, initramfs configuration, Xorg configuration, host files, or NVIDIA driver configuration. Review scripts before running them.

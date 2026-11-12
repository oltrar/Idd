# Idd

Guest-local Linux virtual display for NVIDIA GPU-passthrough VMs.

This repository now contains an **installable guest-side prototype**. It provisions the upstream Hermes-KMS DRM/KMS virtual display driver inside the VM, a systemd lifetime service, and compositor control helpers. The host must not install or run any component from this repository.

## What it provides

- A real DRM/KMS virtual display connector named `HERMES-1` inside the guest.
- Synthetic EDID and modes without an HDMI/DP dummy plug.
- Guest-only installation and lifecycle: the output is connected while the guest service owns it and disconnects when that owner exits.
- The passed-through NVIDIA GPU is explicitly preferred as the compositor's render GPU: KWin receives an NVIDIA-first `KWIN_DRM_DEVICES` order, and Mutter receives its documented preferred-primary-GPU udev tag. Hermes-KMS is a separate virtual display endpoint, not an NVIDIA physical connector. Verify actual rendering with `nvidia-smi` after logging in again.
- KDE Plasma / KWin and GNOME / Mutter use the guest DRM/KMS output.
- Hyprland uses its compositor-native headless output helper (not a DRM connector; see limitations).

## Install inside the VM

For CachyOS / Arch or Debian / Ubuntu, with the NVIDIA driver installed **inside the guest** and NVIDIA DRM KMS enabled:

```sh
sudo bash ./scripts/install-linux-guest.sh --guest             # KDE / GNOME DRM connector\n# or, for Hyprland's compositor-native output:\nsudo bash ./scripts/install-linux-guest.sh --guest --compositor=hyprland
```

The installer requires a VM marker and the typed confirmation `INSTALL-IN-GUEST`. It builds pinned Hermes-KMS `v0.4.0` against the running guest kernel through DKMS, builds the control utility, and installs/enables the guest systemd service. Matching kernel headers are required.

Default output: 1920x1080@60. Change `/etc/idd/virtual-display.conf` to change the mode.

## Use

```sh
iddctl verify
iddctl status
iddctl activate-kde       # KDE Wayland, from the logged-in user session
iddctl activate-gnome     # opens GNOME Display Settings
iddctl hyprland-start     # Hyprland only, from that user session, no sudo
iddctl hyprland-stop
sudo iddctl stop          # KDE/GNOME: disconnect the guest DRM output
sudo iddctl start         # KDE/GNOME: reconnect the guest DRM output
```

Full instructions, isolation model, expected outputs and known limitations: [docs/linux-guest-display.md](docs/linux-guest-display.md).

## Isolation model

The installer runs only on the OS where it is executed. PCI passthrough is the isolation boundary: the hypervisor assigns the secondary GPU to the VM and keeps its functions bound to VFIO. This project's module creates a separate virtual DRM device in the guest; it does not touch NVIDIA's DP/HDMI connector states, the host's EDID, host DRM, host initramfs, or host display settings. **Never run the installer on the hypervisor.**

## Current limitations

- Hermes-KMS is an external GPL-2.0 kernel-module dependency pinned at `v0.4.0`; this repository integrates and manages it rather than reimplementing the Linux DRM subsystem.
- KWin/Mutter GPU preference is configured guest-locally, but must still be validated on the target driver. NVIDIA's Hermes-KMS capture/import path documents a CPU-copy fallback, so zero-copy NVIDIA streaming is not promised.
- Hyprland's Aquamarine backend does not currently use Hermes-KMS/EVDI as a working secondary DRM output; the helper creates a Hyprland headless compositor output instead. That is not a DRM connector.
- This prototype has not yet been run on the user's RTX 3080 Ti / NVIDIA 615.78.08 / CachyOS 7.3.1 guest. The guest install is the hardware validation step.

Uninstall from inside the guest only:

```sh
sudo bash ./scripts/uninstall-linux-guest.sh --guest
```

# Linux guest-local virtual display: design and setup

## Requirement

The target is the Linux-guest equivalent of Windows IddSampleDriver:

- A virtual monitor is enumerated by the guest's display stack.
- The monitor exists only while the VM/guest device is running.
- The host must never enumerate the virtual monitor.
- The passed-through RTX 30-series NVIDIA GPU is the guest's rendering GPU.
- No HDMI/DP dummy plug and no host-side display/EDID configuration.
- KDE Plasma Wayland, GNOME Wayland, and Hyprland are target compositors.

## Isolation model (non-negotiable)

PCI passthrough is the boundary. On the host, the secondary GPU must be bound to `vfio-pci` and assigned to the VM. The host should not load `nvidia` or `nouveau` for that device and must not create any virtual connector for it.

The virtual display driver/device is installed **inside the guest only**. Its DRM node and connector are therefore guest kernel objects. When the VM is stopped, those guest objects cease to exist; the host does not gain a monitor or connector.

Do not use `drm.edid_firmware`, NVIDIA EDID overrides, Xorg configuration, or a fake connector on the host. Those approaches act on whichever kernel owns the GPU and are specifically outside this project's design.

## Important Linux architecture distinction

Windows IddCx provides an indirect-display framework and a user-mode frame-processing path. Linux has no directly equivalent, cross-compositor API that lets a generic user-mode driver register a monitor and select an arbitrary render GPU.

A virtual DRM/KMS device (for example VKMS or EVDI) can expose a guest-local connector, but that does **not by itself** make that connector part of the NVIDIA DRM device or guarantee that the compositor renders on NVIDIA. VKMS is a software display device. EVDI is a separate virtual DRM device and needs a userspace producer/manager. A compositor may render on one GPU and present to another only when its DRM/GBM/PRIME path supports that arrangement.

The required implementation therefore has two separable parts:

1. **Guest-local display endpoint:** a virtual DRM device/connector with hotplug, EDID/modes, enable/disable, and clean teardown.
2. **NVIDIA rendering path:** ensure the compositor (or a dedicated capture/render process) creates its rendering device on the passed-through NVIDIA GPU and transfers frames to the virtual display endpoint efficiently. Validate PRIME dma-buf import/export and synchronization; do not assume it works merely because `nvidia-smi` sees the GPU.

A connector override on the NVIDIA card itself is not the preferred design: proprietary NVIDIA drivers do not expose a general Linux equivalent of IddCx for arbitrary virtual connectors, and forcing a physical connector connected is driver/version-dependent. It also couples the feature to NVIDIA's connector implementation rather than providing a portable guest driver.

## Guest setup: collect a baseline first

Supported targets for investigation: Arch Linux and Debian, NVIDIA proprietary driver, RTX 30-series. Run the read-only diagnostic:

```sh
chmod +x scripts/guest-gpu-diagnostics.sh
sudo scripts/guest-gpu-diagnostics.sh
```

Record the output before installing a virtual display component. In particular, check:

- `nvidia-smi` sees the passed-through GPU.
- `lspci -nnk` shows the NVIDIA device using the NVIDIA driver in the guest.
- `/sys/module/nvidia_drm/parameters/modeset` is `Y`.
- The DRM card associated with NVIDIA is identified by its PCI device path, not by assuming it is `card0`.
- Existing physical connectors are recorded so tests can prove the virtual endpoint is separate.

## Prototype options (not yet an integrated solution)

### VKMS

VKMS is useful for testing guest-local DRM connector enumeration and compositor hotplug behavior. It is software-only and does not satisfy the NVIDIA-rendering requirement by itself. Use it only as a display-endpoint test, not as the final solution.

### EVDI

EVDI can expose a virtual DRM device and is a more relevant starting point for a userspace-managed virtual display. It is still a separate DRM device, not a connector owned by NVIDIA. A working implementation must provide/manage the EVDI device in the guest and prove that the chosen compositor can render on NVIDIA and present/copy frames to EVDI. Package availability and module signing differ between Arch and Debian; use distribution packages or build against the running kernel, and do not install anything on the host.

### NVIDIA fake connector / EDID override

This is not a portable supported implementation. Driver parameter names and behavior vary by NVIDIA driver branch and kernel module flavor. Do not deploy a guessed `config_file` parameter or kernel EDID override as a general setup recipe. It must be independently verified for the exact guest driver version and must only be applied inside the guest.

## Acceptance tests for a real implementation

A future implementation is not complete until all of these pass:

1. Start VM: guest sees a new DRM connector with a stable name and EDID.
2. Display settings in KDE Wayland, GNOME Wayland, and Hyprland enumerate the monitor and can set supported modes.
3. Render a GPU workload on the passed-through NVIDIA GPU; verify with `nvidia-smi` and compositor/backend diagnostics.
4. Verify frame presentation to the virtual output without a physical plug.
5. Stop the VM: host DRM connector list and host display configuration are byte-for-byte unchanged from before VM startup.
6. Reboot host with VM stopped: no virtual connector appears and no dummy plug is required.
7. Start/stop/restart the guest device repeatedly without stale connectors or compositor crashes.
8. Test both Arch and Debian, documenting kernel, NVIDIA driver, and compositor versions.

## Host-side VFIO sanity check

The host should bind the passthrough GPU and its associated functions (graphics and audio, as applicable) to `vfio-pci`. This is a hypervisor configuration task and is intentionally not automated by this guest project. Never run guest installation steps on the host.

## Current limitation

This initial contribution is diagnostics and architecture documentation, not the virtual display driver itself. The next engineering step is a small guest-only prototype using a virtual DRM endpoint plus an NVIDIA render/presentation test, followed by compositor-specific integration. It would be misleading to advertise a complete IddCx replacement before those tests pass.

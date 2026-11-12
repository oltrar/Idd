# Idd

Linux research/prototype for a guest-local virtual display on a GPU-passthrough VM.

> **Scope and isolation guarantee:** all virtual-display devices described here must be created inside the guest. The host must not load a virtual-display module, create a DRM connector, install an EDID override, or alter its display configuration. When the VM is stopped, the guest's virtual display disappears with the VM. The passed-through GPU must remain assigned to VFIO on the host.

## Status

This repository does **not yet contain a production Linux Indirect Display Driver equivalent**. The first Linux contribution adds guest diagnostics and documents the required architecture and validation gates. It deliberately does not claim that VKMS, EVDI, or an NVIDIA connector override is equivalent to Windows IddCx.

See [Linux guest design and setup](docs/linux-guest-display.md).

## Quick diagnostic

Run inside the Linux guest:

```sh
chmod +x scripts/guest-gpu-diagnostics.sh
sudo scripts/guest-gpu-diagnostics.sh
```

The script is read-only. It reports NVIDIA driver state, DRM devices/connectors, and whether the NVIDIA DRM KMS module is enabled. It does not change the host or guest configuration.

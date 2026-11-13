# Idd — guest-local NVIDIA display for Linux Wayland

Expose a software monitor on an unused connector of the NVIDIA GPU **inside a
PCI-passthrough Linux VM**. No dummy plug, host EDID configuration, Xorg dummy
server, or extra software-rendered DRM card. The output belongs to `nvidia-drm`.

Includes an executable implementation, installer, boot service, hotplug rule,
EDID generator, rollback, and tests. It uses NVIDIA's existing KMS connector-force
and EDID path. An existing `DP-1`/`HDMI-A-1` becomes a connected monitor; a new
connector name is not allocated. `Idd NVIDIA` advertises configurable SDR modes
from `/etc/idd/modes.json`, with 1920×1080/60 Hz preferred initially. This is not
an implementation of Windows IddCx.

**Validation status:** the user has confirmed a working software monitor on
CachyOS/RTX 3080 Ti/615.78.08. Both supplied probe reports show forced `DP-1`
using an active NVIDIA CRTC at 1080p60. The additional high-resolution/refresh
modes are generated and tested in CI; their availability still depends on
NVIDIA's mode validation. See the [guide](docs/linux-guest-display.md).

## Resolutions and refresh rates

Edit `/etc/idd/modes.json`, then apply it:

```sh
sudo nano /etc/idd/modes.json
sudo idd apply
```

Default requested modes:

| Resolution | Refresh rates (Hz) |
| --- | --- |
| 1280×720 | 60 |
| 1920×1080 | 60, 120, 144, 165, 240 |
| 2560×1440 | 60, 120, 144, 165 |
| 3840×2160 | 30, 60, 120 |

Choose an advertised mode in the desktop display settings. `preferred` is a hint
for new desktop configurations; it does not override a mode already saved by
your compositor. The installer preserves edited profiles. Existing original
1080p-only configurations adopt the mode file automatically after upgrading;
explicit custom EDIDs are preserved. See [upgrade and config examples](docs/linux-guest-display.md#configurable-modes-and-upgrading).

## One-run check without installing

When the guest is available, run this from the checkout:

```sh
sudo python3 src/idd.py probe --guest-only
```

`src/idd.py` is self-contained and can also be downloaded on its own. The probe
automatically selects a single NVIDIA GPU and up to two unused connector types,
briefly registers a monitor, waits for the compositor, and restores physical
detection. It saves one `idd-report-*.json` in the current directory. There is
no service installation, package download, desktop restart, or reboot. Existing
Idd displays are inspected without changing them. It never uploads the report.
Encoder assignment is reported separately from unverified moving frames.
See [probe details](docs/linux-guest-display.md#one-run-check-no-installation).

## Install inside the guest

Requires Python 3.9+, systemd/udev, NVIDIA DRM KMS, and accessible DRM debugfs.

```sh
git clone https://github.com/oltrar/Idd.git
cd Idd
# Before merge, check out the implementation branch shown in the PR.
sudo bash scripts/install.sh --guest-only
idd list
# This address and unused port match the supplied 3080 Ti guest report:
sudo idd configure --guest-only --pci 0000:03:00.0 --connector DP-1
sudo systemctl enable --now idd-display.service
sudo idd verify
# After enabling this output in the Wayland desktop:
sudo idd verify --active
```

Read the [full guide](docs/linux-guest-display.md) before removing your temporary
cable. `verify` checks enumeration; `--active` also requires an assigned NVIDIA
encoder. Confirm moving frames through guest capture/remote desktop software.
The service enables the output before the next login screen.

## Remove the monitor

```sh
sudo systemctl disable --now idd-display.service
sudo idd down
```

This restores physical detection and clears Idd's EDID override. No monitor
EEPROM is programmed. With the GPU kept on `vfio-pci` in the host, the guest
monitor ceases to exist when the guest stops.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 src/idd.py edid /tmp/idd.edid
edid-decode --check /tmp/idd.edid
```

`scripts/guest-gpu-diagnostics.sh` remains read-only. No telemetry is uploaded.

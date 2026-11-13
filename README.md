# Idd — guest-local NVIDIA display for Linux Wayland

Expose a software monitor on an unused connector of the NVIDIA GPU **inside a
PCI-passthrough Linux VM**. No dummy plug, host EDID configuration, Xorg dummy
server, or extra software-rendered DRM card. The output belongs to `nvidia-drm`.

Includes an executable implementation, installer, boot service, hotplug rule,
EDID generator, rollback, and tests. It uses NVIDIA's existing KMS connector-force
and EDID path. An existing `DP-1`/`HDMI-A-1` becomes a connected monitor; a new
connector name is not allocated. The default monitor is `Idd NVIDIA`, 1920×1080
at 60 Hz, SDR. This is not an implementation of Windows IddCx.

**Validation status:** control-path tests and CI are provided. Unplugged NVIDIA
scanout and KDE/GNOME/Hyprland hardware acceptance still require the actual guest;
they have not been established by the supplied telemetry. Driver branches and
connector types can behave differently. See the [setup and validation
guide](docs/linux-guest-display.md) for exact checks and limitations.

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

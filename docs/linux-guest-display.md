# NVIDIA display inside a Linux passthrough guest

## Implementation and scope

Idd supplies an EDID and forces one unused NVIDIA connector connected inside
the guest. Wayland compositors discover it through their normal DRM backends.
The display belongs to the NVIDIA connector/encoder/CRTC path; no VKMS/EVDI
device or cross-GPU frame-copy driver is involved.

This is a software monitor on an **existing** `DP-1` or `HDMI-A-1`, not an
implementation of Windows IddCx or an unlimited-output driver. It consumes an
available NVIDIA display head. The default EDID advertises 1920×1080 at 60 Hz,
SDR. A custom EDID can request other modes, subject to NVIDIA validation. HDR,
VRR, HDMI audio, and high-refresh modes are not promised by the default profile.

The supplied guest report identifies an RTX 3080 Ti at `0000:03:00.0`, driver
`615.78.08`, open kernel module (`Dual MIT/GPL`), and `nvidia_drm modeset=Y`.
That exact NVIDIA source implements `DRM_FORCE_ON` → `forceConnected` and EDID
override handling: [connector code](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.78.08/kernel-open/nvidia-drm/nvidia-drm-connector.c)
and [NVKMS bridge](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.78.08/src/nvidia-modeset/kapi/src/nvkms-kapi.c).
This establishes an implementation path, not proof of unplugged scanout.

Additional source review traces the forced state through NVKMS into
`nvDPSetClientForcedConnected` and DisplayPort's `bClientForcedConnected` flag,
which allows attach paths past their usual unplugged-state checks. See the
[NVKMS DP bridge](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.78.08/src/nvidia-modeset/src/dp/nvdp-connector.cpp)
and [DisplayPort connector implementation](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.78.08/src/common/displayport/src/dp_connectorimpl.cpp).
This supports using the driver's existing force path; remaining link/modeset
behavior still requires hardware evidence.

**Arch/CachyOS, Debian, KDE, GNOME, and Hyprland remain hardware acceptance
targets.** The supplied report was taken with HDMI connected. Automated fixtures
cannot establish that a disconnected port will successfully present frames.
DisplayPort link training and driver mode validation may still prevent scanout.

## Guest/host boundary

Run all installation and display commands below **inside the Linux VM**.
The dedicated GPU must already be bound to `vfio-pci` on the hypervisor, with
its other passed-through PCI functions assigned as appropriate. Idd does not
change VFIO, the host bootloader, GPU ROM, or hypervisor display settings.

All settings are guest files or guest-kernel DRM state. With the dedicated GPU
kept isolated on VFIO, the host has no synthetic monitor to discover when the VM
is off. Guest PCI addresses can differ from host addresses.
`systemd-detect-virt` returning `none` does not invalidate your report:
hypervisor identity can be hidden. `--guest-only` records your explicit choice
of installation environment; it cannot independently certify host VFIO binding.

## One-run check (no installation)

There is no need to power on the guest just to collect more information now.
When it is convenient, download `src/idd.py` alone or use the repository checkout
and run **one command inside the guest**:

```sh
sudo python3 src/idd.py probe --guest-only
```

The file is self-contained and uses Python's standard library. If you downloaded
it as `idd-check.py`, the command is `sudo python3 idd-check.py probe --guest-only`.
No installer, extra package, network connection, desktop restart, or reboot is
required. The existing NVIDIA KMS driver must already be loaded.

The probe automatically finds the sole NVIDIA GPU, chooses at most one unused
HDMI and one unused DP connector (DVI if applicable), and tests them sequentially.
Occupied ports and other owners' overrides are left alone. It waits up to eight
seconds per tested port for compositor encoder assignment, then restores physical
detection. A temporary udev rule under `/run` is removed at the end; an initially
unmounted debugfs mount is also removed if possible. There is no persistent
configuration/service change. A temporary monitor may briefly change the desktop
layout as the compositor processes hotplug. An existing Idd-managed output is
only inspected and is left running.

The output is one `idd-report-YYYYMMDD-HHMMSS.json` in the current directory,
owned by the invoking sudo user. It contains NVIDIA/driver identity, kernel
version, before/after connector state, the attempts and cleanup results, atomic
DRM state when available, and bounded GPU-related kernel messages. It collects
no screenshots, user documents, or full process command lines and uploads nothing.
Review the file before sharing it; kernel messages can include system details.
The probe prints the exact saved path.

Possible results distinguish registration failure, enumeration with no compositor
enable, and observed encoder assignment. None claims moving-frame verification.
Running without a Wayland session can check enumeration but cannot prove that a
compositor enables the display. Other checks and source review do not require
repeated guest runs. This report does not test cold boot without a cable or host
VFIO isolation.

If the guest exposes several NVIDIA GPUs, append `--pci 0000:03:00.0` to select
the GPU from the supplied report. `--connector DP-1` restricts the test to one
unused port; the default needs neither option. `--wait 0` skips waiting for a
compositor. If a process is forcibly killed or cleanup fails, its runtime state
is retained for recovery with `sudo python3 src/idd.py down`; guest reboot also
clears the transient display state. No output is configured to start on boot.

## 1. Prepare the guest

Keep SSH, a management console, or the temporary cable available for the first
attempt. Idd registers a monitor; use your chosen guest capture/remote desktop
software to view its pixels. It does not install a streaming server.

Keep your existing distribution NVIDIA driver. Its userspace and kernel modules
must match; Idd does not replace them, build DKMS modules, or patch the kernel.

Arch/CachyOS:

```sh
sudo pacman -S --needed python git
```

Debian:

```sh
sudo apt update
sudo apt install python3 git kmod
```

Both need systemd/udev and a kernel with DRM/debugfs. Check KMS:

```sh
cat /sys/module/nvidia_drm/parameters/modeset
```

It must be `Y` or `1`; your report already passes. If disabled, create
`/etc/modprobe.d/idd-nvidia-kms.conf` **inside the guest** containing:

```text
options nvidia_drm modeset=1
```

If the module is loaded from the initramfs, rebuild the guest initramfs using
the tool that your installation actually uses, then reboot the guest:

| Guest initramfs tool | Rebuild command |
| --- | --- |
| mkinitcpio (Arch) | `sudo mkinitcpio -P` |
| initramfs-tools (Debian) | `sudo update-initramfs -u -k all` |
| dracut (some CachyOS/Arch installs) | `sudo dracut --regenerate-all --force` |

Do not run all three commands. No `video=...` or `drm.edid_firmware=...` boot
parameter is needed. Remove conflicting **guest** connector overrides from
earlier experiments; Idd refuses to replace them.

## 2. Install and select the GPU/port

```sh
git clone https://github.com/oltrar/Idd.git
cd Idd
# Before merge, fetch/check out the implementation branch shown in the PR.
sudo bash scripts/install.sh --guest-only
idd list
```

The installer copies these files and reloads systemd/udev configuration:

| Destination | Purpose |
| --- | --- |
| `/usr/local/bin/idd` | Python executable, standard library only |
| `/etc/systemd/system/idd-display.service` | Enable before the display manager |
| `/etc/udev/rules.d/71-idd-hotplug.rules` | Translate Idd events into DRM hotplug |

The installer does not enable the service or change a connector. Your report has
three disconnected DP ports and occupied HDMI. Start on an unused port:

```sh
sudo idd configure --guest-only --pci 0000:03:00.0 --connector DP-1
sudo systemctl enable --now idd-display.service
sudo idd verify
```

Configuration, including the validated EDID, is stored in `/etc/idd/config.json`.
Idd locates the GPU by PCI identity on each boot. It reads the DRM device minor
to find debugfs and supports the newer PCI-named layout too. It does not assume
`card0` or `card1`.

On start, Idd loads `nvidia_drm`, mounts debugfs if needed, and waits up to
20 seconds for the selected output. It requires a disconnected port, no existing
force, and no existing EDID override. Then it:

1. Saves recovery state under `/run/idd` before changing display controls.
2. Writes the complete EDID to that connector's `edid_override` in one write.
3. Writes `on` to sysfs `status`, forcing and re-probing the display.
4. Checks connection, exact EDID readback, and NVIDIA's advertised modes.
5. Sends a card event that the installed udev rule translates to `HOTPLUG=1`.

The order uses the actual [DRM status handler](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/drm_sysfs.c)
and [EDID override interface](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/drm_debugfs.c).
Writing debugfs `force` alone does not run NVIDIA's mode probe. The udev rule
translates `SYNTH_ARG_IDD` from the kernel's [synthetic uevent mechanism](https://github.com/torvalds/linux/blob/master/lib/kobject_uevent.c);
a bare card `change` event is not assumed to carry DRM hotplug metadata.

Failed registration triggers a restore attempt. If cleanup fails, the state
file remains so `sudo idd down` can retry. Mutations use a process lock. `up` is
idempotent for the same active configuration. `configure` refuses changes while
Idd owns a display; stop it first.

## 3. Enable and verify in Wayland

Use the compositor's native DRM/KMS session, with NVIDIA as its rendering GPU.
If a QXL/virtio virtual graphics adapter is also present, it can affect renderer
selection. After establishing recovery access, prefer making the passed-through
NVIDIA GPU the guest's only graphics adapter. This does not affect the host GPU.

**KDE Plasma:** Enable `Idd NVIDIA` on your selected port under System Settings
→ Display & Monitor, at 1920×1080 60 Hz. Inspect output and renderer information:

```sh
kscreen-doctor -o
qdbus6 org.kde.KWin /KWin supportInformation
```

Look for NVIDIA/RTX 3080 Ti as KWin's OpenGL renderer. Some distributions call
the executable `qdbus` instead. If multiple guest GPUs are intentional,
`KWIN_DRM_DEVICES` is an ordered device list set before KWin launches; its
[DRM backend](https://github.com/KDE/kwin/blob/master/src/backends/drm/drm_backend.cpp)
implements this selection. Use actual guest device paths, not a hardcoded card
number carried across boots.

**Hyprland:** Add a rule using your selected port:

```ini
monitor = DP-1, 1920x1080@60, 0x0, 1
```

```sh
hyprctl reload
hyprctl monitors all
hyprctl systeminfo
```

Check both the monitor and NVIDIA renderer. Modern Hyprland uses Aquamarine;
`AQ_DRM_DEVICES` can select GPUs before compositor startup. Avoid obsolete
`WLR_*` recipes for an Aquamarine session. If NVIDIA is the only guest DRM GPU,
no selection variable should be needed.

**GNOME:** In a GNOME Wayland session, open Settings → Displays and enable
`Idd NVIDIA` at 1920×1080. Resolve any guest GDM configuration disabling Wayland
first. With multiple accelerated guest GPUs, check Mutter's renderer selection
in its session logs. GNOME Remote Desktop's separate virtual-session mechanism
is not this output.

After enabling the output:

```sh
sudo idd verify --active
nvidia-smi
```

`verify` checks NVIDIA ownership, connection, EDID, and modes. `--active` also
requires an encoder assignment (`enabled`). Neither proves that frames advance
or that every application/compositor renders on NVIDIA. Check compositor
renderer information and view moving content via guest KMS/PipeWire capture or
your remote desktop. `nvidia-smi` or an Xwayland-only OpenGL tool alone cannot
establish the Wayland compositor's rendering device.

## 4. Remove the cable and test persistence

1. Confirm the software monitor appears beside the temporary physical display.
   Test moving frames on the software monitor.
2. Unplug the temporary HDMI cable. Confirm `sudo idd verify --active` and
   continued capture from the software monitor.
3. Reboot the **guest** with no cable connected. Verify the login screen and
   desktop output. The service runs before `display-manager.service`.
4. Stop/start the service three times through SSH or another independent
   console. Check disappearance, reappearance, and moving frames each time.
5. Test each compositor you use; record its result separately.

If DP enumerates but presentation fails with link-training or atomic-modeset
errors, try the now-unused HDMI port:

```sh
sudo systemctl stop idd-display.service
# Unplug HDMI before configuring this target.
sudo idd configure --guest-only --pci 0000:03:00.0 --connector HDMI-A-1
sudo systemctl start idd-display.service
sudo idd verify --active
```

This uses no dummy plug. If both port types fail unplugged scanout, this driver
path has not satisfied the requirement: keep the logs rather than treating
`connected` as success. A separate generic DRM module cannot simply attach a
connector to NVIDIA's private display engine.

## Custom EDID

The built-in EDID needs no physical monitor. To reuse another monitor's modes,
capture its EDID in the guest before unplugging it. Determine its current card
using `idd list`; `card1` here is just an example:

```sh
cat /sys/class/drm/card1-HDMI-A-1/edid > monitor.edid
sudo systemctl stop idd-display.service
sudo idd configure --guest-only --pci 0000:03:00.0 --connector DP-1 --edid monitor.edid
sudo systemctl start idd-display.service
```

Validation checks the header, version, length, extension count, and every block
checksum. NVIDIA still validates modes. HDMI modes are not guaranteed to work
on a forced DP output, nor are advertised high-refresh/HDR features guaranteed.

## Troubleshooting and rollback

```sh
sudo systemctl status idd-display.service --no-pager
sudo journalctl -b -u idd-display.service --no-pager
sudo journalctl -b -k --no-pager | grep -Ei 'nvidia|drm|edid|modeset|link'
sudo bash scripts/guest-gpu-diagnostics.sh
sudo idd status
```

- **Controls unavailable / permission denied:** Check guest debugfs support and
  kernel lockdown/security policy. Root may still be denied override writes.
  Idd reports failure and does not change Secure Boot policy.
- **Already forced / existing override:** Remove the conflicting guest setup
  using the tool that installed it. Do not combine `video=DP-1:e` with Idd.
- **Connected, scanout disabled:** Enable the output in the compositor, check
  its rendering device, and inspect NVIDIA modeset/link errors.
- **Live hotplug missed:** Reload the installed udev rule with
  `sudo udevadm control --reload-rules`. Observe events with
  `sudo udevadm monitor --udev --property --subsystem-match=drm`; an Idd event
  should contain `SYNTH_ARG_IDD=1` and `HOTPLUG=1`. Guest reboot also tests
  enumeration before the compositor starts.
- **Boot never reaches Linux without a cable:** Idd starts in Linux userspace.
  Firmware, VM startup policy, GPU reset, and pre-kernel boot problems need
  hypervisor/firmware investigation.

Disable and restore normal detection:

```sh
sudo systemctl disable --now idd-display.service
sudo idd down
```

If the desktop cannot start, use SSH/TTY. A one-boot recovery option is
`systemd.mask=idd-display.service` on the **guest** kernel command line. A guest
kernel reboot clears runtime force/EDID state; the service recreates it only
when started.

After disabling, optionally uninstall:

```sh
sudo rm -f /usr/local/bin/idd /etc/systemd/system/idd-display.service
sudo rm -f /etc/udev/rules.d/71-idd-hotplug.rules /etc/idd/config.json
sudo systemctl daemon-reload
sudo udevadm control --reload-rules
```

## Acceptance record

| Check | Evidence | Status |
| --- | --- | --- |
| Control logic and EDID | Unit tests, independent EDID decode | Automated in CI |
| Arch/CachyOS and Debian | Kernel/driver versions, unplugged output and frames | Pending hardware run |
| KDE, GNOME, Hyprland | Display settings, NVIDIA renderer, moving frames | Pending each compositor |
| No cable after guest reboot | Login screen, session and capture | Pending hardware run |
| Teardown/restart | Three service cycles and VM shutdown | Pending hardware run |
| Host isolation | Unchanged host DRM/layout, dedicated GPU stays VFIO-bound | Host-side audit required |

Compare host connector names/status/EDIDs and layout before VM startup and after
shutdown, and verify the dedicated GPU stays on `vfio-pci`. Do not install Idd
on the host for this check. Guest telemetry cannot prove host isolation.

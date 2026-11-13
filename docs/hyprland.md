# Hyprland integration

Hyprland uses the same NVIDIA DRM connector and EDID as KDE/GNOME. Idd adds a
user-session helper to choose a mode, enable that output, and persist its native
Hyprland monitor rule. It supports Hyprlang and newer Lua configurations through
their respective `hyprctl keyword monitor` and `hyprctl eval` interfaces.

## Upgrade and set up

Update the repository checkout and run the guest installer. Before this PR
merges, select its `codex/hyprland-integration` branch; after merge, update main.

```sh
sudo bash scripts/install.sh --guest-only
```

Your existing `idd-display.service`, GPU/port selection, and EDID profile remain
in place. If the service is not already enabled:

```sh
sudo systemctl enable --now idd-display.service
```

Start a native Hyprland Wayland session, then run **without sudo** in one of its
terminals:

```sh
idd hyprland setup --mode 2560x1440@144
```

Use `idd hyprland setup` with no mode to select the EDID's preferred mode. The
helper detects the `Idd NVIDIA` model, selects only that monitor, checks specific
rates against Hyprland's `availableModes`, writes the native include, reloads
the config, applies the monitor rule, and verifies the compositor's selected
resolution/rate/scale. EDID rounding such as 143.998 Hz is accepted for 144 Hz.
It does not create a Hyprland headless output or another DRM GPU.

The main config is backed up beside the original file before its managed source
block is added or moved. Existing unrelated monitor rules remain in place.
If a new config error, rejected command, or mode-verification failure occurs,
Idd attempts to restore the previous files and monitor rule. Existing config
errors must be fixed before setup/apply, so a broken config is not treated as
successful persistence.

## Your settings files

| File | Purpose | Apply command |
| --- | --- | --- |
| `/etc/idd/modes.json` | Resolutions and rates advertised by NVIDIA to every compositor | `sudo idd apply` |
| `~/.config/idd/hyprland.json` | Hyprland's chosen mode, position and scale | `idd hyprland apply` |

The helper follows `XDG_CONFIG_HOME` when set. The selected native config path
and format are recorded in the user profile. Edit `mode`, `position`, or `scale`
there, then apply it. For example:

```json
{
  "connector": "DP-1",
  "mode": "2560x1440@144",
  "position": "auto",
  "scale": 1,
  "format": "hyprlang",
  "config": "/home/your-user/.config/hypr/hyprland.conf"
}
```

The actual `config` value is generated using your real path; keep that value.
Choose `preferred` or a mode Hyprland advertises. Position may be `auto` or an
explicit offset such as `1920x0`. Scale defaults to 1; Hyprland must accept the
scale for the chosen resolution. You can also change these through setup:

```sh
idd hyprland setup --mode 1920x1080@240 --position 1920x0 --scale 1
# Negative positions require this argument form:
idd hyprland setup --position=-1920x0
```

The generated `hyprland-monitor.conf`/`.lua` is under `~/.config/idd` and is
sourced from your main config. It contains a native rule for the chosen port,
so Hyprland applies it at startup and when the guest output is hotplugged by
`sudo idd apply`. A `preferred` rule follows the EDID preference; an explicit
resolution/rate remains the selected rule until you change it. If you remove a
selected mode from the EDID, choose another supported mode in the user profile.

## Hyprlang, Lua and custom configs

Setup automatically finds a single existing `hyprland.conf` or `hyprland.lua`
under your Hyprland config directory. If both exist, or you launch with a custom
`--config`, point to the file your running session actually uses:

```sh
idd hyprland setup --config ~/.config/hypr/hyprland.lua --mode 2560x1440@144
```

Lua setup writes `hl.monitor({...})` and adds a `require(...)` block. Hyprlang
setup writes `monitor = ...` and adds `source = ...`. File suffix selects the
format; use `--format lua` or `--format hyprlang` for a custom filename whose
suffix does not describe its syntax. The helper does not convert your existing
configuration between languages.

For a second instance or a same-user SSH terminal, select an instance explicitly:

```sh
hyprctl instances
idd hyprland status --instance YOUR_INSTANCE_SIGNATURE
idd hyprland apply --instance YOUR_INSTANCE_SIGNATURE
```

Ordinary calls use `HYPRLAND_INSTANCE_SIGNATURE`. The root display service does
not enter user IPC sessions. Run `idd hyprland` as the logged-in desktop user.
The standalone `idd-hyprland` command exposes the same helper.

## NVIDIA GPU visibility

`hyprctl monitors all` must show the Idd output. If it does not, first check that
the guest display service is running and `sudo idd verify` succeeds. If Hyprland
opened only a virtio/QXL/other guest GPU, select NVIDIA before starting Hyprland.
Setting an environment variable after the compositor has started cannot replace
its DRM backend.

For the GPU address in the supplied guest reports, a manual TTY launch is:

```sh
AQ_DRM_DEVICES="$(readlink -f /dev/dri/by-path/pci-0000:03:00.0-card)" start-hyprland
```

Older releases may use `Hyprland` instead of `start-hyprland`. Resolve the PCI
symlink on each launch as above rather than hardcoding `card1`. Its resolved
path avoids the colons in PCI symlink names, which conflict with
`AQ_DRM_DEVICES`' list separator. For a display manager, configure this variable
in the environment of the session launcher. If several guest GPUs are needed,
include their device paths in priority order, with NVIDIA first for rendering.
All of this applies to the guest; the host's dedicated passthrough GPU remains
on VFIO. No host display configuration is installed.

## Status and removal

```sh
idd hyprland status
```

Status includes Hyprland's actual resolution, refresh, scale, position, disabled
state and available mode list. It reports the compositor state; it does not
assert that captured frames advance or that every application renders on NVIDIA.

To remove only the user-session integration:

```sh
idd hyprland remove
```

This removes the managed source block, generated include and user profile, then
reloads Hyprland. Other monitor rules and the guest NVIDIA display service remain
under their existing configuration. To remove the guest monitor as well, use
`sudo systemctl disable --now idd-display.service` and `sudo idd down`.

If a config needs manual recovery, use its timestamped `.idd-backup-*` sibling.
If the session cannot connect, the generated source block is marked
`BEGIN IDD HYPRLAND`/`END IDD HYPRLAND` for manual removal. A recovery error from
the helper identifies any restore operation that could not complete.

Reference: Hyprland's official [hyprctl guide](https://wiki.hypr.land/configuring/core/advanced-configuration/using-hyprctl/),
[Lua config guide](https://wiki.hypr.land/configuring/core/),
[monitor API](https://wiki.hypr.land/configuring/core/monitors/),
[versioned Hyprlang monitor syntax](https://wiki.hypr.land/0.51.0/Configuring/Monitors/),
and [GPU selection guide](https://wiki.hypr.land/configuring/extra/multi-gpu/).

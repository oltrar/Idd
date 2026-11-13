#!/usr/bin/python3
"""Guest-local NVIDIA DRM display. Python 3.9+, standard library only."""

import argparse
import contextlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


class IddError(Exception):
    pass


def validate_target(pci, connector):
    if not isinstance(pci, str) or not re.fullmatch(
        r"[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]", pci
    ):
        raise IddError("Use a guest PCI address such as 0000:03:00.0.")
    if not isinstance(connector, str) or not re.fullmatch(
        r"(?:DP|HDMI-A|DVI-D)-[1-9][0-9]*", connector
    ):
        raise IddError("Use one physical connector name, for example DP-1 or HDMI-A-1.")


def validate_edid(data):
    if len(data) < 128 or len(data) > 1024 or len(data) % 128:
        raise IddError("EDID must contain 1 to 8 complete 128-byte blocks.")
    if data[:8] != bytes.fromhex("00ffffffffffff00"):
        raise IddError("Invalid EDID header.")
    if len(data) != (data[126] + 1) * 128:
        raise IddError("EDID extension count does not match its length.")
    if any(sum(data[i:i + 128]) % 256 for i in range(0, len(data), 128)):
        raise IddError("Invalid EDID block checksum.")
    if data[18:20] != b"\x01\x04" and data[18:20] != b"\x01\x03":
        raise IddError("Only EDID 1.3 and 1.4 are supported.")
    return data


def make_edid():
    """EDID 1.4, 1920x1080 at 60 Hz, 148.5 MHz, digital RGB, SDR."""
    e = bytearray(128)
    e[:8] = bytes.fromhex("00ffffffffffff00")
    e[8:10] = ((9 << 10) | (4 << 5) | 4).to_bytes(2, "big")  # IDD
    e[10:12] = (1).to_bytes(2, "little")
    e[12:16] = (1).to_bytes(4, "little")
    e[16:25] = bytes([1, 36, 1, 4, 0xA0, 52, 29, 120, 0x06])
    chroma = [round(v * 1024) for v in (.64, .33, .30, .60, .15, .06, .3127, .3290)]
    e[25] = sum((chroma[i] & 3) << (6 - i * 2) for i in range(4))
    e[26] = sum((chroma[i + 4] & 3) << (6 - i * 2) for i in range(4))
    e[27:35] = bytes(v >> 2 for v in chroma)
    e[38:54] = b"\x01\x01" * 8
    # CTA 1080p60 timing: H 1920/88/44/148, V 1080/4/5/36.
    e[54:72] = bytes.fromhex("023a801871382d40582c450008222100001e")
    e[72:90] = b"\x00\x00\x00\xfc\x00" + b"Idd NVIDIA\n  "
    e[90:108] = b"\x00\x00\x00\xff\x00" + b"GuestOnly-1\n "
    # 59..61 Hz, 67..68 kHz, 150 MHz, range only (no extra timings).
    e[108:126] = bytes.fromhex("000000fd003b3d43440f010a202020202020")
    e[127] = (-sum(e)) & 255
    return validate_edid(bytes(e))


DEFAULT_MODES = {
    "preferred": "1920x1080@60",
    "modes": [
        {"width": 1280, "height": 720, "refresh_rates": [60]},
        {"width": 1920, "height": 1080, "refresh_rates": [60, 120, 144, 165, 240]},
        {"width": 2560, "height": 1440, "refresh_rates": [60, 120, 144, 165]},
        {"width": 3840, "height": 2160, "refresh_rates": [30, 60, 120]},
    ],
}


def mode_name(width, height, refresh):
    return f"{width}x{height}@{refresh:g}"


def mode_timing(width, height, refresh, rb=2):
    """Progressive CVT reduced blanking; EDID clocks are quantized to 10 kHz."""
    if (width, height, refresh) == (1920, 1080, 60):
        # Keep the known-working initial monitor's preferred timing unchanged.
        hblank, hfront, hsync, vblank, vfront, vsync, clock, flags = 280, 88, 44, 45, 4, 5, 148500, 0x1e
    else:
        # CVT: at least 460 us vertical blanking. RBv2 has 80 clocks H blank,
        # 8-clock H front porch, 8-line V sync, and 6-line V back porch.
        period = (1000000 / refresh - 460) / height
        hblank, hfront, hsync = (80, 8, 32) if rb == 2 else (160, 48, 32)
        if rb == 2:
            vsync = 8
            vblank = max(math.floor(460 / period) + 1, 15)
            vfront = vblank - vsync - 6
            clock = math.floor(refresh * (height + vblank) * (width + hblank) / 10000) * 10
        else:
            vsync = next((s for x, y, s in ((4, 3, 4), (16, 9, 5), (16, 10, 6),
                                           (5, 4, 7), (15, 9, 7)) if width * y == height * x), 10)
            vfront = 3
            vblank = max(math.floor(460 / period) + 1, vfront + vsync + 7)
            clock = math.floor(refresh * (height + vblank) * (width + hblank) / 250000) * 250
        flags = 0x1a  # digital separate sync, +H/-V
    return {"width": width, "height": height, "refresh_hz": refresh,
            "clock_khz": clock, "hblank": hblank, "hfront": hfront, "hsync": hsync,
            "vblank": vblank, "vfront": vfront, "vsync": vsync, "flags": flags,
            "actual_hz": clock * 1000 / ((width + hblank) * (height + vblank))}


def legacy_dtd(t):
    if (t["clock_khz"] > 655350 or max(t["width"], t["height"], t["hblank"], t["vblank"]) > 4095
            or max(t["vfront"], t["vsync"]) > 63):
        return None
    w, h, hb, vb = (t[key] for key in ("width", "height", "hblank", "vblank"))
    hp, hs, vp, vs = (t[key] for key in ("hfront", "hsync", "vfront", "vsync"))
    d = bytearray(18)
    d[:2] = (t["clock_khz"] // 10).to_bytes(2, "little")
    d[2:8] = bytes([w & 255, hb & 255, ((w >> 8) << 4) | (hb >> 8),
                     h & 255, vb & 255, ((h >> 8) << 4) | (vb >> 8)])
    d[8:12] = bytes([hp & 255, hs & 255, ((vp & 15) << 4) | (vs & 15),
                      ((hp >> 8) << 6) | ((hs >> 8) << 4) | ((vp >> 4) << 2) | (vs >> 4)])
    d[12:15] = bytes.fromhex("082221")  # 520 x 290 mm
    d[17] = t["flags"]
    return bytes(d)


def displayid_dtd(t, preferred=False):
    # DisplayID 1.3 Type I: a 24-bit 10 kHz clock and 16-bit dimensions.
    # This avoids truncating 1440p165/4K120 into the 16-bit legacy clock field.
    data = bytearray((t["clock_khz"] // 10 - 1).to_bytes(3, "little"))
    data.append(0x04)  # 16:9 aspect (custom ratios below use undefined).
    if t["width"] * 9 != t["height"] * 16:
        data[3] = 0x08
    if preferred:
        data[3] |= 0x80
    for key in ("width", "hblank", "hfront", "hsync", "height", "vblank", "vfront", "vsync"):
        value = t[key] - 1
        if key == "hfront" and t["flags"] & 2:
            value |= 0x8000
        if key == "vfront" and t["flags"] & 4:
            value |= 0x8000
        data.extend(value.to_bytes(2, "little"))
    return bytes(data)


def profile_timings(profile):
    if not isinstance(profile, dict) or set(profile) != {"preferred", "modes"}:
        raise IddError("Mode config must have exactly preferred and modes keys.")
    if not isinstance(profile["modes"], list) or not 1 <= len(profile["modes"]) <= 24:
        raise IddError("modes must contain 1 to 24 resolution entries.")
    timings = {}
    for entry in profile["modes"]:
        if not isinstance(entry, dict) or set(entry) != {"width", "height", "refresh_rates"}:
            raise IddError("Each mode needs width, height, and refresh_rates.")
        width, height = entry["width"], entry["height"]
        if type(width) is not int or type(height) is not int or not (320 <= width <= 8192 and 200 <= height <= 8192):
            raise IddError("Mode dimensions must be integers: width 320..8192, height 200..8192.")
        rates = entry["refresh_rates"]
        if not isinstance(rates, list) or not 1 <= len(rates) <= 24:
            raise IddError("refresh_rates must be a nonempty list (maximum 24 rates).")
        for rate in rates:
            if type(rate) not in (int, float) or not math.isfinite(rate) or not 24 <= rate <= 480:
                raise IddError("Refresh rates must be finite numbers between 24 and 480 Hz.")
            name = mode_name(width, height, rate)
            if name in timings:
                raise IddError(f"Duplicate mode: {name}")
            timings[name] = mode_timing(width, height, rate)
    if len(timings) > 24:
        raise IddError("At most 24 resolution/refresh combinations are supported.")
    preferred = profile["preferred"]
    if not isinstance(preferred, str) or preferred not in timings:
        raise IddError("preferred must match a listed mode, for example 1920x1080@60.")
    if legacy_dtd(timings[preferred]) is None:
        # RBv1's short vertical front porch fits legacy DTDs for more modes.
        t = timings[preferred]
        timings[preferred] = mode_timing(t["width"], t["height"], t["refresh_hz"], rb=1)
        if legacy_dtd(timings[preferred]) is None:
            raise IddError("Preferred timing exceeds legacy EDID limits; use 1080p60 or 4K60 as preferred. "
                           "Higher modes can still be listed and selected in the desktop.")
    return [timings[preferred]] + [t for name, t in timings.items() if name != preferred]


def modes_edid(profile):
    timings = profile_timings(profile)
    e = bytearray(make_edid())
    e[12:16] = b"\0" * 4  # the string serial descriptor is authoritative
    e[54:72] = legacy_dtd(timings[0])
    legacy, extended = [], []
    for t in timings[1:]:
        d = legacy_dtd(t)
        (legacy if d is not None else extended).append(d if d is not None else displayid_dtd(t))
    ranges = [math.floor(min(t["actual_hz"] for t in timings)), math.ceil(max(t["actual_hz"] for t in timings)),
              math.floor(min(t["clock_khz"] / (t["width"] + t["hblank"]) for t in timings)),
              math.ceil(max(t["clock_khz"] / (t["width"] + t["hblank"]) for t in timings))]
    max_clock = math.ceil(max(t["clock_khz"] for t in timings) / 10000)
    if legacy:
        # CTA requires the VGA compatibility timing and selectable RGB range.
        e[35] |= 0x20  # established 640x480 at 60 Hz
        ranges[0], ranges[1] = min(ranges[0], 59), max(ranges[1], 60)
        ranges[2], ranges[3] = min(ranges[2], 31), max(ranges[3], 32)
        max_clock = max(max_clock, 3)
    if max(ranges) > 510 or max_clock > 255:
        raise IddError("Requested modes exceed EDID range limits (510 Hz/kHz or 2550 MHz pixel clock).")
    offsets = sum(1 << i for i, value in enumerate(ranges) if value > 255)
    e[112:119] = bytes([offsets, *(v - 255 if v > 255 else v for v in ranges), max_clock, 1])
    extensions = []
    for i in range(0, len(legacy), 6):
        block = bytearray(128)
        start = 7 if i == 0 else 4
        block[:4] = bytes([0x02, 0x03, start, 0x80])
        if i == 0:
            block[4:7] = bytes([0xe2, 0, 0x4a])  # one global VCDB: RGB range selectable, underscan
        payload = b"".join(legacy[i:i + 6])
        block[start:start + len(payload)] = payload
        block[127] = (-sum(block)) & 255
        extensions.append(block)
    sections = []
    if extended:
        # DisplayID is a complete structure even when carried by an EDID. Its
        # first section identifies the monitor and repeats the same preferred
        # timing as the base EDID; drivers deduplicate that identical timing.
        name = b"Idd NVIDIA"
        product = b"IDD" + (1).to_bytes(2, "little") + b"\0" * 4 + bytes([1, 26, len(name)]) + name
        native = timings[0]
        params = ((5200).to_bytes(2, "little") + (2900).to_bytes(2, "little")
                  + native["width"].to_bytes(2, "little") + native["height"].to_bytes(2, "little")
                  + bytes([0, 120, min(255, max(0, round(native["width"] / native["height"] * 100) - 100)), 0x77]))
        # Generic digital interface for the software sink, RGB 8 bpc, no audio,
        # content protection or claimed physical-link bandwidth capabilities.
        interface = bytes([0xb1, 0x10, 0x02, 0, 0, 0, 0, 0, 0, 0])
        metadata = (bytes([0, 0, len(product)]) + product + bytes([1, 0, len(params)]) + params
                    + bytes([0x0f, 0, len(interface)]) + interface)
        records = [displayid_dtd(native, preferred=True)] + extended
        sections.append((metadata, records[:3]))
        sections.extend((b"", records[i:i + 5]) for i in range(3, len(records), 5))
    for i, (metadata, records) in enumerate(sections):
        payload = b"".join(records)
        body = metadata + bytes([0x03, 0, len(payload)]) + payload
        section = bytearray([0x13, len(body), 3 if i == 0 else 0, len(sections) - 1 if i == 0 else 0]) + body
        section.append((-sum(section)) & 255)  # DisplayID's own checksum
        block = bytearray(128)
        block[0] = 0x70
        block[1:1 + len(section)] = section
        block[127] = (-sum(block)) & 255      # outer EDID checksum
        extensions.append(block)
    e[126] = len(extensions)
    e[127] = (-sum(e[:127])) & 255
    return validate_edid(bytes(e) + b"".join(extensions))


def mode_summary(profile):
    return [{"mode": mode_name(t["width"], t["height"], t["refresh_hz"]),
             "encoded_refresh_hz": round(t["actual_hz"], 4), "pixel_clock_mhz": t["clock_khz"] / 1000}
            for t in profile_timings(profile)]


def preferred_size(data):
    d = data[54:72]
    if int.from_bytes(d[:2], "little") == 0:
        return None
    return f"{d[2] + ((d[4] >> 4) << 8)}x{d[5] + ((d[7] >> 4) << 8)}"


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)


class Kernel:
    def __init__(self, sys=Path("/sys"), debug=Path("/sys/kernel/debug/dri"),
                 config=Path("/etc/idd/config.json"), run=Path("/run/idd")):
        self.sys, self.debug, self.config, self.run = sys, debug, config, run

    @property
    def state(self):
        return self.run / "state.json"

    def write(self, path, data):
        # sysfs/debugfs handlers consume a single write, including binary EDID.
        fd = os.open(path, os.O_WRONLY)
        try:
            if os.write(fd, data) != len(data):
                raise IddError(f"Short kernel write: {path}")
        finally:
            os.close(fd)

    def cards(self):
        result = []
        for card in sorted((self.sys / "class/drm").glob("card*")):
            if not re.fullmatch(r"card[0-9]+", card.name):
                continue
            uevent = card / "device/uevent"
            if not uevent.exists():
                continue
            info = dict(line.split("=", 1) for line in uevent.read_text().splitlines() if "=" in line)
            if info.get("DRIVER") == "nvidia" and info.get("PCI_ID", "").startswith("10DE:"):
                result.append((card, info["PCI_SLOT_NAME"]))
        return result

    def target(self, cfg, debug=True):
        validate_target(cfg["pci"], cfg["connector"])
        cards = [c for c, pci in self.cards() if pci == cfg["pci"]]
        if len(cards) != 1:
            raise IddError(f"NVIDIA DRM card for {cfg['pci']} is unavailable.")
        card = cards[0]
        connector = card.with_name(card.name + "-" + cfg["connector"])
        if not (connector / "status").exists():
            raise IddError(f"Connector {cfg['connector']} is unavailable on {cfg['pci']}.")
        if not debug:
            return card, connector, None
        minor = (card / "dev").read_text().strip().split(":")[1]
        if not minor.isdecimal():
            raise IddError("Invalid DRM device minor.")
        for base in (self.debug / minor, self.debug / cfg["pci"]):
            endpoint = base / cfg["connector"]
            if (endpoint / "edid_override").exists() and (endpoint / "force").exists():
                return card, connector, endpoint
        raise IddError("DRM EDID/force controls unavailable. Check debugfs and kernel lockdown.")

    def check_kms(self):
        path = self.sys / "module/nvidia_drm/parameters/modeset"
        if not path.exists() or path.read_text().strip() not in ("Y", "1"):
            raise IddError("NVIDIA DRM KMS is disabled. Enable nvidia_drm modeset=1 in the guest.")

    def load(self):
        cfg = json.loads(self.config.read_text())
        if cfg.get("guest_only") is not True:
            raise IddError("Missing guest-only acknowledgement. Run idd configure inside the VM.")
        validate_target(cfg.get("pci"), cfg.get("connector"))
        default_file = self.config.parent / "modes.json"
        if ("modes_file" not in cfg and cfg.get("edid_source") != "custom"
                and cfg.get("edid") == make_edid().hex() and default_file.exists()):
            # Seamless upgrade of the original 1080p60-only configuration.
            cfg["modes_file"] = str(default_file)
        if "modes_file" in cfg:
            path = Path(cfg["modes_file"])
            if not path.is_absolute():
                raise IddError("modes_file must be an absolute guest path.")
            profile = json.loads(path.read_text())
            cfg["edid"] = modes_edid(profile).hex()
            cfg["requested_modes"] = mode_summary(profile)
        else:
            validate_edid(bytes.fromhex(cfg["edid"]))
        return cfg

    def notify(self, card):
        # The shipped udev rule translates SYNTH_ARG_IDD to HOTPLUG=1.
        self.write(card / "uevent", f"change {uuid.uuid4()} IDD=1\n".encode())

    @contextlib.contextmanager
    def locked(self):
        import fcntl
        self.run.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.run / "lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield


def configure(k, pci, connector, edid=None, modes_file=None):
    validate_target(pci, connector)
    if edid is not None and modes_file is not None:
        raise IddError("Choose a custom EDID or a modes file, not both.")
    if k.state.exists():
        raise IddError("Run idd down before changing configuration.")
    cfg = {"guest_only": True, "pci": pci, "connector": connector}
    profile_path = None
    if edid is not None:
        cfg.update(edid=validate_edid(edid).hex(), edid_source="custom")
    else:
        profile_path = Path(modes_file).resolve() if modes_file is not None else k.config.parent / "modes.json"
        profile = json.loads(profile_path.read_text()) if profile_path.exists() else DEFAULT_MODES
        if modes_file is not None and not profile_path.exists():
            raise IddError(f"Mode config does not exist: {profile_path}")
        modes_edid(profile)  # validate before any configuration writes
        cfg["modes_file"] = str(profile_path)
    k.check_kms()
    _, output, _ = k.target(cfg, debug=False)
    if (output / "status").read_text().strip() != "disconnected":
        raise IddError("Select an unused, disconnected connector. Unplug its cable first.")
    if profile_path is not None and not profile_path.exists():
        atomic_write(profile_path, (json.dumps(profile, indent=2) + "\n").encode(), mode=0o644)
    atomic_write(k.config, (json.dumps(cfg, indent=2) + "\n").encode())
    return k.load()


def report(k, cfg):
    card, output, _ = k.target(cfg, debug=False)
    data = (output / "edid").read_bytes()
    result = {
        "pci": cfg["pci"], "card": card.name, "connector": cfg["connector"],
        "driver": "nvidia", "status": (output / "status").read_text().strip(),
        "enabled": (output / "enabled").read_text().strip(),
        "edid_bytes": len(data), "edid_matches": data == bytes.fromhex(cfg["edid"]),
        "modes": (output / "modes").read_text().splitlines(),
    }
    if "requested_modes" in cfg:
        result["requested_modes"] = cfg["requested_modes"]
        result["refresh_note"] = "sysfs modes lists resolutions only; use desktop display settings for accepted refresh rates."
    return result


def verify(k, cfg, active=False):
    k.check_kms()
    r = report(k, cfg)
    if r["status"] != "connected" or not r["edid_matches"] or not r["modes"]:
        raise IddError("NVIDIA did not expose the requested display/EDID/modes: " + json.dumps(r))
    preferred = preferred_size(bytes.fromhex(cfg["edid"]))
    if preferred and preferred not in r["modes"]:
        raise IddError(f"NVIDIA rejected the preferred mode {preferred}: {r['modes']}")
    if active and r["enabled"] != "enabled":
        raise IddError("Display enumerated, but the compositor has not enabled scanout: " + json.dumps(r))
    return r


def restore(k, state):
    card, output, endpoint = k.target(state)
    # Disconnect first so the compositor stops using the output, then restore EDID.
    original = bytes.fromhex(state["original_edid"])
    force = {"unspecified": "detect", "on": "on", "digital": "on-digital", "off": "off"}
    operations = [
        lambda: k.write(output / "status", b"off\n"),
        lambda: k.notify(card),
        lambda: k.write(endpoint / "edid_override", original or b"reset"),
        lambda: k.write(output / "status", (force[state["original_force"]] + "\n").encode()),
        lambda: k.notify(card),
    ]
    errors = []
    for operation in operations:
        try:
            operation()
        except (OSError, IddError) as error:
            errors.append(str(error))
    if errors:
        raise IddError("Restore incomplete: " + "; ".join(errors))


def up(k, cfg):
    k.check_kms()
    card, output, endpoint = k.target(cfg)
    if k.state.exists():
        state = json.loads(k.state.read_text())
        if any(state.get(key) != cfg[key] for key in ("pci", "connector", "edid")):
            raise IddError("A different display is managed already. Run idd down first.")
        return verify(k, cfg)
    original_force = (endpoint / "force").read_text().strip()
    original_edid = (endpoint / "edid_override").read_bytes()
    if original_force != "unspecified" or original_edid:
        raise IddError("Connector already has a force/EDID override; refusing to replace it.")
    if (output / "status").read_text().strip() != "disconnected":
        raise IddError("Connector is occupied; refusing to replace a physical display.")
    state = dict(cfg, original_force=original_force, original_edid=original_edid.hex())
    atomic_write(k.state, (json.dumps(state) + "\n").encode())
    try:
        k.write(endpoint / "edid_override", bytes.fromhex(cfg["edid"]))
        # status=on both sets DRM_FORCE_ON and calls fill_modes/driver force.
        # Writing debugfs/force alone would not re-probe the NVIDIA display.
        k.write(output / "status", b"on\n")
        result = verify(k, cfg)
        k.notify(card)
        return result
    except (OSError, IddError):
        try:
            restore(k, state)
        except (OSError, IddError) as error:
            raise IddError(f"Enable failed and rollback needs attention: {error}. Run idd down.")
        k.state.unlink()
        raise


def down(k):
    if not k.state.exists():
        return {"managed": False}
    state = json.loads(k.state.read_text())
    restore(k, state)
    k.state.unlink()
    return {"managed": False, "restored": state["connector"], "pci": state["pci"]}


def apply_config(k, cfg):
    """Validate before unplugging; restore the previous EDID if registration fails."""
    old = json.loads(k.state.read_text()) if k.state.exists() else None
    if old is not None and all(old.get(key) == cfg[key] for key in ("pci", "connector", "edid")):
        return up(k, cfg)
    if old is not None:
        down(k)
    try:
        return up(k, cfg)
    except (OSError, IddError) as error:
        if old is not None and not k.state.exists():
            try:
                up(k, old)
            except (OSError, IddError) as recovery_error:
                raise IddError(f"New modes failed: {error}; previous display recovery failed: {recovery_error}")
            raise IddError(f"New modes failed; previous display restored: {error}")
        raise


def prepare(k, timeout):
    subprocess.run(["modprobe", "nvidia_drm"], check=True)
    if not os.path.ismount("/sys/kernel/debug"):
        subprocess.run(["mount", "-t", "debugfs", "-o", "nosuid,nodev,noexec",
                        "debugfs", "/sys/kernel/debug"], check=True)
    deadline = time.monotonic() + timeout
    cfg = k.load()
    while True:
        try:
            k.check_kms()
            k.target(cfg)
            return cfg
        except (OSError, IddError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def snapshot(k):
    """Only GPU display state; no EDID serials, user files or screenshots."""
    cards = []
    for card, pci in k.cards():
        outputs = []
        for path in sorted(card.parent.glob(card.name + "-*/status")):
            output = path.parent
            try:
                outputs.append({
                    "connector": output.name[len(card.name) + 1:],
                    "status": path.read_text().strip(),
                    "enabled": (output / "enabled").read_text().strip(),
                    "modes": (output / "modes").read_text().splitlines(),
                    "edid_bytes": len((output / "edid").read_bytes()),
                })
            except OSError as error:
                outputs.append({"connector": output.name, "error": str(error)})
        cards.append({"pci": pci, "card": card.name, "connectors": outputs})
    return cards


@contextlib.contextmanager
def probe_hotplug():
    """A temporary /run rule supports a source-file probe without installation."""
    rule = Path("/run/udev/rules.d") / ("71-idd-probe-" + uuid.uuid4().hex + ".rules")
    rule.parent.mkdir(parents=True, exist_ok=True)
    content = ('ACTION=="change", SUBSYSTEM=="drm", KERNEL=="card[0-9]*", '
               'DRIVERS=="nvidia", ENV{SYNTH_ARG_IDD}=="1", ENV{HOTPLUG}="1"\n')
    atomic_write(rule, content.encode(), mode=0o644)
    try:
        subprocess.run(["udevadm", "control", "--reload-rules"], check=True, timeout=5)
        yield
    finally:
        rule.unlink(missing_ok=True)
        subprocess.run(["udevadm", "control", "--reload-rules"], check=True, timeout=5)


def probe(k, result, pci=None, connector=None, wait=8):
    """Caller holds the normal Idd lock; always undo displays created here."""
    result.update(before=snapshot(k), attempts=[], frames_verified=False)
    k.check_kms()
    if k.state.exists():
        # Inspect rather than tear down a display owned by a previous invocation.
        state = json.loads(k.state.read_text())
        result["existing_managed_display"] = report(k, state)
        result["outcome"] = "existing_display_inspected"
        result["after"] = snapshot(k)
        return
    cards = [(card, address) for card, address in k.cards() if pci is None or address == pci]
    if len(cards) != 1:
        raise IddError("Select one NVIDIA GPU with --pci; none or several matched.")
    card, pci = cards[0]
    candidates = []
    for family in ("HDMI-A-", "DP-", "DVI-D-"):
        for path in sorted(card.parent.glob(card.name + "-" + family + "*/status")):
            name = path.parent.name[len(card.name) + 1:]
            if connector is not None and name != connector:
                continue
            if path.read_text().strip() == "disconnected":
                candidates.append(name)
                break  # At most one unused port per type, max two attempts below.
    if not candidates:
        raise IddError("No unused matching connector; physical displays were left alone.")
    with probe_hotplug():
        for name in candidates[:2]:
            cfg = {"guest_only": True, "pci": pci, "connector": name, "edid": make_edid().hex()}
            attempt = {"pci": pci, "connector": name}
            result["attempts"].append(attempt)
            try:
                attempt["enumerated"] = up(k, cfg)
                deadline = time.monotonic() + wait
                while True:
                    observed = report(k, cfg)
                    attempt["observed"] = observed
                    if observed["enabled"] == "enabled" or time.monotonic() >= deadline:
                        break
                    time.sleep(0.25)
                attempt["encoder_assigned"] = observed["enabled"] == "enabled"
                _, _, endpoint = k.target(cfg)
                state_file = endpoint.parent / "state"
                if state_file.exists():
                    with state_file.open(errors="replace") as f:
                        attempt["drm_atomic_state"] = f.read(65536)
            except (OSError, IddError) as error:
                attempt["error"] = str(error)
            finally:
                # up() only saves state after it has passed the occupied/override
                # guards. Thus an unsuccessful preflight owns nothing to undo.
                if k.state.exists():
                    try:
                        down(k)
                    except (OSError, IddError) as error:
                        attempt["cleanup_error"] = str(error)
                attempt["recovery_state_remaining"] = k.state.exists()
            if attempt["recovery_state_remaining"]:
                break
    result["after"] = snapshot(k)
    if k.state.exists():
        result["outcome"] = "cleanup_required"
    elif any(a.get("encoder_assigned") for a in result["attempts"]):
        result["outcome"] = "encoder_assigned_frames_unverified"
    elif any("enumerated" in a for a in result["attempts"]):
        result["outcome"] = "enumerated_compositor_did_not_enable"
    else:
        result["outcome"] = "registration_failed"


def bounded_command(argv):
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace", timeout=10)
        return {"exit_code": r.returncode, "output": r.stdout[-32768:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": str(error)}


def run_probe(k, args):
    """Save a single report even after a test error or keyboard interruption."""
    # Reserve the destination before touching display state. Never overwrite an
    # existing file or discover an invalid report path after the hardware test.
    report_fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    report_file = os.fdopen(report_fd, "w", encoding="utf-8")
    result = {"schema": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
              "outcome": "preflight_failed", "frames_verified": False,
              "kernel": os.uname().release,
              "nvidia": bounded_command(["nvidia-smi", "--query-gpu=name,driver_version,pci.bus_id",
                                          "--format=csv,noheader"])}
    debug_was_mounted = os.path.ismount("/sys/kernel/debug")
    old_handler = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Probe interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with k.locked():
            # Do not load modules or alter persistent configuration for a probe.
            k.check_kms()
            if not debug_was_mounted:
                subprocess.run(["mount", "-t", "debugfs", "-o", "nosuid,nodev,noexec",
                                "debugfs", "/sys/kernel/debug"], check=True, timeout=5)
            probe(k, result, args.pci, args.connector, args.wait)
    except (OSError, IddError, ValueError, KeyError, KeyboardInterrupt,
            subprocess.SubprocessError) as error:
        result["error"] = str(error) or "Probe interrupted"
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        if not debug_was_mounted and os.path.ismount("/sys/kernel/debug"):
            cleanup = bounded_command(["umount", "/sys/kernel/debug"])
            if cleanup.get("exit_code") != 0:
                result["debugfs_cleanup"] = cleanup
        logs = bounded_command(["journalctl", "-b", "-k", "--no-pager", "-o", "cat", "-n", "1500"])
        if "output" in logs:
            result["gpu_kernel_messages"] = "\n".join(
                line for line in logs["output"].splitlines()
                if re.search(r"nvidia|NVRM|\bdrm\b|edid|modeset", line, re.I))[-16384:]
        result["finished_utc"] = datetime.now(timezone.utc).isoformat()
        with report_file as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        if os.environ.get("SUDO_UID", "").isdecimal() and os.environ.get("SUDO_GID", "").isdecimal():
            os.chown(args.output, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    print(f"Report saved: {args.output.resolve()}\nResult: {result['outcome']}")
    if k.state.exists() and result["outcome"] != "existing_display_inspected":
        print("Recovery state remains. Run sudo python3 " + sys.argv[0] + " down", file=sys.stderr)
    return 0 if result["outcome"] in ("existing_display_inspected", "encoder_assigned_frames_unverified") else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hyprland":
        # Keep the standalone GPU/EDID/probe commands dependency-free.
        candidates = [Path(__file__).with_name("idd_hyprland.py"), Path(__file__).with_name("idd-hyprland")]
        helper = next((path for path in candidates if path.is_file()), None)
        if helper is None:
            print("idd: reinstall from the current checkout to add the Hyprland helper.", file=sys.stderr)
            return 1
        return subprocess.run([sys.executable, str(helper), *argv[1:]]).returncode
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("hyprland", help="Set up/apply/status/remove native Hyprland monitor settings (no sudo)")
    sub.add_parser("list", help="Read-only NVIDIA cards/connectors")
    e = sub.add_parser("edid", help="Write a legacy or configurable multi-mode EDID")
    e.add_argument("output", type=Path)
    e.add_argument("--modes", type=Path, help="JSON mode profile; omit for the legacy 1080p60 EDID")
    m = sub.add_parser("modes", help="Validate and show the configured resolutions and refresh rates")
    m.add_argument("--file", type=Path, help="Preview a JSON profile without a GPU or installation")
    c = sub.add_parser("configure", help="Select the GPU and unused port inside the guest")
    c.add_argument("--guest-only", required=True, action="store_true")
    c.add_argument("--pci", required=True)
    c.add_argument("--connector", required=True)
    source = c.add_mutually_exclusive_group()
    source.add_argument("--edid", type=Path, help="Use an existing monitor EDID instead of generated modes")
    source.add_argument("--modes", type=Path, help="JSON profile; default is /etc/idd/modes.json")
    u = sub.add_parser("up", help="Enable the configured NVIDIA output")
    u.add_argument("--timeout", type=float, default=20)
    a = sub.add_parser("apply", help="Apply modes.json edits, restoring the previous display if registration fails")
    a.add_argument("--timeout", type=float, default=20)
    sub.add_parser("down", help="Restore normal physical connection detection")
    sub.add_parser("status", help="Read-only configured output state")
    v = sub.add_parser("verify", help="Check NVIDIA ownership, EDID and advertised modes")
    v.add_argument("--active", action="store_true", help="Also require compositor-enabled scanout")
    probe_parser = sub.add_parser("probe", help="One-run guest test/report; temporary display, no installation")
    probe_parser.add_argument("--guest-only", required=True, action="store_true")
    probe_parser.add_argument("--pci", help="Auto-select when exactly one NVIDIA GPU is present")
    probe_parser.add_argument("--connector", help="Optional unused port; auto-select up to two port types")
    probe_parser.add_argument("--wait", type=float, default=8, help="Seconds per port for compositor detection (0..15)")
    probe_parser.add_argument("--output", type=Path, default=Path("idd-report-" +
                              datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".json"))
    args = p.parse_args(argv)
    try:
        if args.command == "edid":
            data = modes_edid(json.loads(args.modes.read_text())) if args.modes else make_edid()
            with args.output.open("xb") as f:
                f.write(data)
            print(f"Wrote {args.output} ({len(data)} bytes, SDR)")
            return 0
        if args.command == "modes" and args.file:
            profile = json.loads(args.file.read_text())
            data = modes_edid(profile)
            print(json.dumps({"file": str(args.file), "preferred": profile["preferred"],
                              "edid_bytes": len(data), "requested_modes": mode_summary(profile)}, indent=2))
            return 0
        if sys.platform != "linux":
            raise IddError("Run display commands inside the Linux guest.")
        k = Kernel()
        if args.command == "probe":
            if os.geteuid() != 0:
                raise IddError("The temporary display test requires root inside the guest.")
            if not 0 <= args.wait <= 15:
                raise IddError("Wait must be between 0 and 15 seconds.")
            if args.pci is not None:
                validate_target(args.pci, args.connector or "DP-1")
            elif args.connector is not None:
                validate_target("0000:00:00.0", args.connector)
            if args.output.exists():
                raise IddError("Report file already exists; select a new --output path.")
            return run_probe(k, args)
        if args.command in ("configure", "up", "down", "apply"):
            if os.geteuid() != 0:
                raise IddError("This command requires root inside the guest.")
            with k.locked():
                if args.command == "configure":
                    data = args.edid.read_bytes() if args.edid else None
                    result = configure(k, args.pci, args.connector, data, args.modes)
                    result = {key: value for key, value in result.items() if key not in ("edid", "requested_modes")}
                elif args.command in ("up", "apply"):
                    if not 0 <= args.timeout <= 30:
                        raise IddError("Timeout must be between 0 and 30 seconds.")
                    cfg = prepare(k, args.timeout)
                    result = apply_config(k, cfg) if args.command == "apply" else up(k, cfg)
                else:
                    result = down(k)
        elif args.command == "list":
            result = []
            for card, pci in k.cards():
                outputs = []
                for path in sorted(card.parent.glob(card.name + "-*/status")):
                    outputs.append({"connector": path.parent.name[len(card.name) + 1:],
                                    "status": path.read_text().strip()})
                result.append({"pci": pci, "card": card.name, "connectors": outputs})
        elif args.command == "modes":
            cfg = k.load()
            result = {"modes_file": cfg.get("modes_file"), "edid_source": cfg.get("edid_source", "generated"),
                      "edid_bytes": len(bytes.fromhex(cfg["edid"])), "requested_modes": cfg.get("requested_modes", [])}
        elif args.command == "status":
            result = report(k, k.load())
        else:
            result = verify(k, k.load(), args.active)
        print(json.dumps(result, indent=2))
        return 0
    except (IddError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"idd: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

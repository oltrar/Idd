"""Control-path tests with a simulated DRM kernel; these do not test GPU scanout."""
import importlib.util
import contextlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("idd", Path(__file__).parents[1] / "src/idd.py")
idd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idd)


def decode_timings(data):
    """Decode the on-wire EDID records independently of the encoder helpers."""
    found = []

    def legacy(d):
        clock = int.from_bytes(d[:2], "little") * 10000
        if not clock:
            return
        width, height = d[2] + (d[4] >> 4) * 256, d[5] + (d[7] >> 4) * 256
        ht = width + d[3] + (d[4] & 15) * 256
        vt = height + d[6] + (d[7] & 15) * 256
        found.append((width, height, clock / ht / vt, clock))

    for offset in range(54, 126, 18):
        legacy(data[offset:offset + 18])
    for offset in range(128, len(data), 128):
        block = data[offset:offset + 128]
        if block[0] == 0x02:
            for index in range(block[2], 110, 18):
                legacy(block[index:index + 18])
        elif block[0] == 0x70:
            assert sum(block[1:6 + block[2]]) % 256 == 0
            assert block[5] == 0x03
            for index in range(8, 8 + block[7], 20):
                d = block[index:index + 20]
                clock = (int.from_bytes(d[:3], "little") + 1) * 10000
                values = [int.from_bytes(d[i:i + 2], "little") + 1 for i in range(4, 20, 2)]
                width, hb, _, _, height, vb, _, _ = values
                found.append((width, height, clock / (width + hb) / (height + vb), clock))
    return found


class FakeKernel(idd.Kernel):
    def __init__(self, root):
        super().__init__(root / "sys", root / "debug/dri", root / "etc/config.json", root / "run")
        self.writes = []
        self.reject_modes = False
        self.fail_on = None
        self.fail_notify = False
        self.add_card("card0", "0000:01:00.0", "virtio-pci", "1AF4:1050", 0)
        self.card, self.output, self.endpoint = self.add_card(
            "card7", "0000:03:00.0", "nvidia", "10DE:2208", 5)
        self.put(self.sys / "module/nvidia_drm/parameters/modeset", b"Y\n")

    @staticmethod
    def put(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def add_card(self, name, pci, driver, vendor, minor):
        card = self.sys / "class/drm" / name
        self.put(card / "device/uevent", f"DRIVER={driver}\nPCI_ID={vendor}\nPCI_SLOT_NAME={pci}\n".encode())
        self.put(card / "dev", f"226:{minor}\n".encode())
        self.put(card / "uevent", b"")
        output = card.with_name(name + "-DP-1")
        for key, value in {"status": b"disconnected\n", "edid": b"", "modes": b"", "enabled": b"disabled\n"}.items():
            self.put(output / key, value)
        endpoint = self.debug / str(minor) / "DP-1"
        self.put(endpoint / "force", b"unspecified\n")
        self.put(endpoint / "edid_override", b"")
        return card, output, endpoint

    def write(self, path, data):
        self.writes.append((path, data))
        if (path, data) == self.fail_on:
            raise OSError("injected kernel write failure")
        if path == self.output / "status":
            if data == b"on\n":
                self.put(self.endpoint / "force", b"on\n")
                self.put(path, b"connected\n")
                self.put(self.output / "edid", (self.endpoint / "edid_override").read_bytes())
                self.put(self.output / "modes", b"" if self.reject_modes else b"1920x1080\n")
            else:
                self.put(self.endpoint / "force", b"off\n" if data == b"off\n" else b"unspecified\n")
                self.put(path, b"disconnected\n")
                self.put(self.output / "edid", b"")
                self.put(self.output / "modes", b"")
                self.put(self.output / "enabled", b"disabled\n")
        elif path == self.endpoint / "edid_override":
            self.put(path, b"" if data == b"reset" else data)
        elif path == self.card / "uevent":
            if self.fail_notify:
                raise OSError("injected event failure")
            self.put(path, data)
        else:
            raise AssertionError(f"Unexpected write {path}")


class EdidTests(unittest.TestCase):
    def test_default_modes_all_survive_binary_encoding(self):
        data = idd.modes_edid(idd.DEFAULT_MODES)
        decoded = decode_timings(data)
        expected = {(m["width"], m["height"], rate) for m in idd.DEFAULT_MODES["modes"] for rate in m["refresh_rates"]}
        self.assertEqual({(w, h, round(hz)) for w, h, hz, _ in decoded}, expected)
        self.assertEqual(len(decoded), len(expected))
        self.assertGreater(len(data), 128)
        self.assertEqual(data[126], len(data) // 128 - 1)
        for index in range(0, len(data), 128):
            self.assertEqual(sum(data[index:index + 128]) % 256, 0)

    def test_high_pixel_clocks_use_displayid_without_overflow(self):
        decoded = decode_timings(idd.modes_edid(idd.DEFAULT_MODES))
        mode = next(t for t in decoded if t[:2] == (3840, 2160) and round(t[2]) == 120)
        self.assertGreater(mode[3], 1000000000)
        self.assertAlmostEqual(mode[2], 120, delta=0.01)
        qhd = next(t for t in decoded if t[:2] == (2560, 1440) and round(t[2]) == 165)
        self.assertGreater(qhd[3], 655350000)

    def test_preferred_1440p144_is_first_and_fits_legacy_descriptor(self):
        profile = dict(idd.DEFAULT_MODES, preferred="2560x1440@144")
        first = decode_timings(idd.modes_edid(profile))[0]
        self.assertEqual(first[:2], (2560, 1440))
        self.assertAlmostEqual(first[2], 144, delta=0.1)

    def test_fractional_refresh_and_nonstandard_dimensions(self):
        profile = {"preferred": "1920x1080@59.94", "modes": [
            {"width": 1920, "height": 1080, "refresh_rates": [59.94]},
            {"width": 3440, "height": 1440, "refresh_rates": [100]}]}
        timings = decode_timings(idd.modes_edid(profile))
        self.assertAlmostEqual(timings[0][2], 59.94, delta=0.01)
        self.assertEqual(timings[1][:2], (3440, 1440))

    def test_range_descriptor_covers_all_advertised_timings(self):
        data = idd.modes_edid(idd.DEFAULT_MODES)
        r = data[108:126]
        limits = [r[5 + i] + (255 if r[4] & (1 << i) else 0) for i in range(4)]
        for t in idd.profile_timings(idd.DEFAULT_MODES):
            self.assertLessEqual(limits[0], t["actual_hz"])
            self.assertGreaterEqual(limits[1], t["actual_hz"])
            khz = t["clock_khz"] / (t["width"] + t["hblank"])
            self.assertLessEqual(limits[2], khz)
            self.assertGreaterEqual(limits[3], khz)
            self.assertGreaterEqual(r[9] * 10000, t["clock_khz"])

    def test_maximum_mode_count_across_extension_blocks(self):
        profile = {"preferred": "1920x1080@60", "modes": [
            {"width": 1920, "height": 1080, "refresh_rates": [60]},
            {"width": 3840, "height": 2160, "refresh_rates": list(range(83, 106))}]}
        data = idd.modes_edid(profile)
        self.assertEqual(len(decode_timings(data)), 24)
        self.assertLessEqual(len(data), 1024)

    def test_invalid_profiles_are_rejected(self):
        base = {"preferred": "1920x1080@60", "modes": [{"width": 1920, "height": 1080, "refresh_rates": [60]}]}
        invalid = [None, {}, dict(base, typo=1), dict(base, preferred="1920x1080@144"), dict(base, modes=[])]
        for rates in ([True], [0], [float("nan")], [float("inf")], [60, 60], ["144"], []):
            invalid.append(dict(base, modes=[dict(base["modes"][0], refresh_rates=rates)]))
        invalid.append(dict(base, modes=[dict(base["modes"][0], width=True)]))
        invalid.append(dict(base, preferred="3840x2160@120", modes=[
            {"width": 3840, "height": 2160, "refresh_rates": [120]}]))
        for profile in invalid:
            with self.subTest(profile=profile), self.assertRaises(idd.IddError):
                idd.modes_edid(profile)

    def test_1080p_timing_and_checksums(self):
        e = idd.make_edid()
        self.assertEqual(len(e), 128)
        self.assertEqual(sum(e) % 256, 0)
        self.assertEqual(idd.preferred_size(e), "1920x1080")
        d = e[54:72]
        hblank = d[3] + ((d[4] & 15) << 8)
        vblank = d[6] + ((d[7] & 15) << 8)
        clock = int.from_bytes(d[:2], "little") * 10000
        self.assertEqual(clock / ((1920 + hblank) * (1080 + vblank)), 60)
        self.assertIn(b"Idd NVIDIA\n", e)

    def test_bad_edids(self):
        e = bytearray(idd.make_edid())
        e[20] ^= 1
        for data in (b"", bytes(e), idd.make_edid() + b"\0", b"x" * 128):
            with self.subTest(length=len(data)), self.assertRaises(idd.IddError):
                idd.validate_edid(data)

    def test_extension_count_and_checksum(self):
        e = bytearray(idd.make_edid())
        e[126] = 1
        e[127] = (-sum(e[:127])) & 255
        with self.assertRaises(idd.IddError):
            idd.validate_edid(e)
        ext = bytearray(128)
        ext[:4] = b"\x02\x03\x04\x00"
        ext[127] = (-sum(ext)) & 255
        self.assertEqual(len(idd.validate_edid(e + ext)), 256)
        ext[5] = 1
        with self.assertRaises(idd.IddError):
            idd.validate_edid(e + ext)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.k = FakeKernel(Path(self.temp.name))
        self.cfg = idd.configure(self.k, "0000:03:00.0", "DP-1", idd.make_edid())

    def test_pci_selection_uses_drm_minor_not_card_number(self):
        card, output, endpoint = self.k.target(self.cfg)
        self.assertEqual(card.name, "card7")
        self.assertEqual(endpoint.parent.name, "5")
        self.assertEqual(output.name, "card7-DP-1")
        self.assertEqual(len(self.k.cards()), 1)

    def test_default_configuration_creates_an_editable_profile(self):
        cfg = idd.configure(self.k, "0000:03:00.0", "DP-1")
        path = self.k.config.parent / "modes.json"
        self.assertEqual(json.loads(path.read_text()), idd.DEFAULT_MODES)
        self.assertEqual(cfg["modes_file"], str(path))
        self.assertEqual(len(cfg["requested_modes"]), 13)
        profile = {"preferred": "1920x1080@60", "modes": [
            {"width": 1920, "height": 1080, "refresh_rates": [60, 120]}]}
        path.write_text(json.dumps(profile))
        reloaded = self.k.load()
        self.assertNotEqual(cfg["edid"], reloaded["edid"])
        self.assertEqual(len(reloaded["requested_modes"]), 2)

    def test_original_1080p_configuration_upgrades_but_custom_edid_stays(self):
        path = self.k.config.parent / "modes.json"
        path.write_text(json.dumps(idd.DEFAULT_MODES))
        self.assertEqual(self.k.load()["edid"], idd.make_edid().hex())  # explicit --edid
        original = dict(self.cfg)
        original.pop("edid_source")
        self.k.config.write_text(json.dumps(original))
        before = self.k.config.read_bytes()
        loaded = self.k.load()
        self.assertEqual(len(loaded["requested_modes"]), 13)
        self.assertEqual(self.k.config.read_bytes(), before)

    def test_apply_changes_active_edid_without_reconfiguration(self):
        idd.up(self.k, self.cfg)
        cfg = dict(self.cfg, edid=idd.modes_edid(idd.DEFAULT_MODES).hex())
        result = idd.apply_config(self.k, cfg)
        self.assertTrue(result["edid_matches"])
        self.assertGreater(result["edid_bytes"], 128)
        self.assertEqual(json.loads(self.k.state.read_text())["edid"], cfg["edid"])

    def test_failed_apply_restores_previous_display(self):
        idd.up(self.k, self.cfg)
        cfg = dict(self.cfg, edid=idd.modes_edid(idd.DEFAULT_MODES).hex())
        actual_write = self.k.write
        failed = False

        def fail_new_mode_once(path, data):
            nonlocal failed
            if path == self.k.output / "status" and data == b"on\n" and not failed:
                failed = True
                raise OSError("new EDID rejected")
            actual_write(path, data)

        with mock.patch.object(self.k, "write", side_effect=fail_new_mode_once):
            with self.assertRaisesRegex(idd.IddError, "previous display restored"):
                idd.apply_config(self.k, cfg)
        self.assertEqual(json.loads(self.k.state.read_text())["edid"], self.cfg["edid"])
        self.assertTrue(idd.verify(self.k, self.cfg)["edid_matches"])

    def test_invalid_edited_profile_does_not_disconnect_active_display(self):
        idd.up(self.k, self.cfg)
        path = self.k.config.parent / "modes.json"
        path.write_text('{"modes": "invalid"}')
        self.k.config.write_text(json.dumps(dict(self.cfg, modes_file=str(path))))
        writes = len(self.k.writes)
        with self.assertRaises(idd.IddError):
            idd.apply_config(self.k, self.k.load())
        self.assertEqual(len(self.k.writes), writes)
        self.assertTrue(idd.verify(self.k, self.cfg)["edid_matches"])

    @unittest.skipIf(os.name == "nt", "PCI directory names require a POSIX filesystem")
    def test_pci_debugfs_layout(self):
        (self.k.debug / "5").rename(self.k.debug / "0000:03:00.0")
        self.assertEqual(self.k.target(self.cfg)[2].parent.name, "0000:03:00.0")

    def test_enable_disable_and_restart(self):
        for _ in range(3):
            r = idd.up(self.k, self.cfg)
            self.assertTrue(r["edid_matches"])
            self.assertEqual(r["driver"], "nvidia")
            self.assertEqual(self.k.writes[-3][0].name, "edid_override")
            self.assertEqual(self.k.writes[-2][1], b"on\n")
            self.assertRegex(self.k.writes[-1][1].decode(), r"change [0-9a-f-]{36} IDD=1\n")
            before = len(self.k.writes)
            idd.up(self.k, self.cfg)
            self.assertEqual(len(self.k.writes), before)
            idd.down(self.k)
            self.assertFalse(self.k.state.exists())
            self.assertEqual((self.k.endpoint / "force").read_text().strip(), "unspecified")
            self.assertEqual((self.k.endpoint / "edid_override").read_bytes(), b"")
        self.assertEqual(idd.down(self.k), {"managed": False})

    def test_verify_requires_active_scanout_separately(self):
        idd.up(self.k, self.cfg)
        with self.assertRaisesRegex(idd.IddError, "scanout"):
            idd.verify(self.k, self.cfg, active=True)
        self.k.put(self.k.output / "enabled", b"enabled\n")
        self.assertEqual(idd.verify(self.k, self.cfg, active=True)["enabled"], "enabled")

    def test_rejected_modes_roll_back(self):
        self.k.reject_modes = True
        with self.assertRaises(idd.IddError):
            idd.up(self.k, self.cfg)
        self.assertFalse(self.k.state.exists())
        self.assertEqual((self.k.endpoint / "edid_override").read_bytes(), b"")
        self.assertEqual((self.k.output / "status").read_text().strip(), "disconnected")

    def test_failed_force_rolls_back_edid(self):
        self.k.fail_on = (self.k.output / "status", b"on\n")
        with self.assertRaises(OSError):
            idd.up(self.k, self.cfg)
        self.assertFalse(self.k.state.exists())
        self.assertEqual((self.k.endpoint / "edid_override").read_bytes(), b"")

    def test_failed_notification_still_restores_kernel_and_retains_recovery_state(self):
        self.k.fail_notify = True
        with self.assertRaisesRegex(idd.IddError, "rollback"):
            idd.up(self.k, self.cfg)
        self.assertTrue(self.k.state.exists())
        self.assertEqual((self.k.endpoint / "edid_override").read_bytes(), b"")
        self.assertEqual((self.k.endpoint / "force").read_text().strip(), "unspecified")
        self.k.fail_notify = False
        idd.down(self.k)
        self.assertFalse(self.k.state.exists())

    def test_refuses_occupied_connector_without_writes(self):
        self.k.put(self.k.output / "status", b"connected\n")
        with self.assertRaisesRegex(idd.IddError, "occupied"):
            idd.up(self.k, self.cfg)
        self.assertFalse(self.k.writes)

    def test_refuses_existing_overrides(self):
        for name, data in (("force", b"on\n"), ("edid_override", idd.make_edid())):
            with self.subTest(control=name):
                self.k.put(self.k.endpoint / name, data)
                with self.assertRaisesRegex(idd.IddError, "already"):
                    idd.up(self.k, self.cfg)
                self.assertFalse(self.k.writes)
                self.k.put(self.k.endpoint / name, b"unspecified\n" if name == "force" else b"")

    def test_reconfigure_active_display_refused(self):
        idd.up(self.k, self.cfg)
        with self.assertRaisesRegex(idd.IddError, "down"):
            idd.configure(self.k, "0000:03:00.0", "DP-2", idd.make_edid())

    def test_configuration_requires_acknowledgement(self):
        self.assertTrue(self.k.load()["guest_only"])
        cfg = dict(self.cfg, guest_only=False)
        self.k.config.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(idd.IddError, "guest-only"):
            self.k.load()

    def test_missing_kms_or_controls_fail_before_writes(self):
        self.k.put(self.k.sys / "module/nvidia_drm/parameters/modeset", b"N\n")
        with self.assertRaisesRegex(idd.IddError, "KMS"):
            idd.up(self.k, self.cfg)
        self.k.put(self.k.sys / "module/nvidia_drm/parameters/modeset", b"Y\n")
        (self.k.endpoint / "edid_override").unlink()
        with self.assertRaisesRegex(idd.IddError, "controls"):
            idd.up(self.k, self.cfg)
        self.assertFalse(self.k.writes)

    def test_target_validation_rejects_paths(self):
        for pci, conn in (("../../host", "DP-1"), ("0000:03:00.0", "../DP-1"),
                          ("0000:03:00.0", "DP-1/../../DP-2")):
            with self.assertRaises(idd.IddError):
                idd.validate_target(pci, conn)

    def do_probe(self, result=None, **kwargs):
        result = {} if result is None else result
        with mock.patch.object(idd, "probe_hotplug", return_value=contextlib.nullcontext()):
            idd.probe(self.k, result, wait=0, **kwargs)
        return result

    def test_probe_needs_no_persistent_configuration(self):
        before = self.k.config.read_bytes()
        self.k.config.unlink()
        result = self.do_probe()
        self.assertEqual(result["outcome"], "enumerated_compositor_did_not_enable")
        self.assertEqual(result["before"], result["after"])
        self.assertFalse(self.k.config.exists())
        self.assertFalse(self.k.state.exists())
        self.assertTrue(before)
        self.assertFalse(result["frames_verified"])

    def test_probe_observes_encoder_after_hotplug_and_restores(self):
        actual_notify = self.k.notify

        def compositor_notified(card):
            actual_notify(card)
            if (self.k.output / "status").read_text().strip() == "connected":
                self.k.put(self.k.output / "enabled", b"enabled\n")

        with mock.patch.object(self.k, "notify", side_effect=compositor_notified):
            result = self.do_probe()
        self.assertEqual(result["outcome"], "encoder_assigned_frames_unverified")
        self.assertEqual(result["before"], result["after"])
        self.assertFalse(self.k.state.exists())

    def test_probe_leaves_existing_managed_display_untouched(self):
        idd.up(self.k, self.cfg)
        writes = len(self.k.writes)
        with mock.patch.object(idd, "probe_hotplug", side_effect=AssertionError("Must not change rules")):
            result = {}
            idd.probe(self.k, result)
        self.assertEqual(result["outcome"], "existing_display_inspected")
        self.assertEqual(len(self.k.writes), writes)
        self.assertTrue(self.k.state.exists())

    def test_probe_failed_registration_also_restores(self):
        self.k.reject_modes = True
        result = self.do_probe()
        self.assertEqual(result["outcome"], "registration_failed")
        self.assertEqual(result["before"], result["after"])
        self.assertIn("error", result["attempts"][0])
        self.assertFalse(self.k.state.exists())

    def test_probe_never_replaces_an_occupied_monitor(self):
        self.k.put(self.k.output / "status", b"connected\n")
        with self.assertRaisesRegex(idd.IddError, "unused"):
            self.do_probe()
        self.assertFalse(self.k.writes)

    def test_probe_does_not_reset_someone_elses_override(self):
        self.k.put(self.k.endpoint / "force", b"off\n")
        result = self.do_probe()
        self.assertEqual(result["outcome"], "registration_failed")
        self.assertFalse(self.k.writes)
        self.assertEqual((self.k.endpoint / "force").read_bytes(), b"off\n")

    def test_probe_requires_explicit_pci_for_multiple_nvidia_gpus(self):
        self.k.add_card("card8", "0000:04:00.0", "nvidia", "10DE:2208", 6)
        with self.assertRaisesRegex(idd.IddError, "--pci"):
            self.do_probe()
        self.assertFalse(self.k.writes)
        result = self.do_probe(pci="0000:03:00.0")
        self.assertEqual(result["attempts"][0]["pci"], "0000:03:00.0")

    def test_probe_keyboard_interruption_still_restores(self):
        with mock.patch.object(idd, "probe_hotplug", return_value=contextlib.nullcontext()), \
             mock.patch.object(idd.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                idd.probe(self.k, {}, wait=1)
        self.assertFalse(self.k.state.exists())
        self.assertEqual((self.k.endpoint / "edid_override").read_bytes(), b"")

    def test_probe_cleanup_failure_preserves_recovery_state(self):
        self.k.fail_notify = True
        result = self.do_probe()
        self.assertEqual(result["outcome"], "cleanup_required")
        self.assertTrue(result["attempts"][0]["recovery_state_remaining"])
        self.assertIn("cleanup_error", result["attempts"][0])

    def test_probe_preflight_error_still_saves_one_report(self):
        args = SimpleNamespace(output=Path(self.temp.name) / "report.json",
                               pci=None, connector=None, wait=0)
        with mock.patch.object(self.k, "locked", return_value=contextlib.nullcontext()), \
             mock.patch.object(self.k, "check_kms", side_effect=idd.IddError("No NVIDIA KMS")), \
             mock.patch.object(idd.os, "uname", create=True, return_value=SimpleNamespace(release="test")), \
             mock.patch.object(idd.os.path, "ismount", return_value=True), \
             mock.patch.object(idd, "bounded_command", return_value={"exit_code": 0, "output": ""}), \
             mock.patch.object(idd.os, "environ", {}):
            self.assertEqual(idd.run_probe(self.k, args), 1)
        saved = json.loads(args.output.read_text())
        self.assertEqual(saved["outcome"], "preflight_failed")
        self.assertEqual(saved["error"], "No NVIDIA KMS")
        self.assertFalse(saved["frames_verified"])
        self.assertFalse(self.k.writes)

    def test_probe_does_not_overwrite_an_existing_report(self):
        path = Path(self.temp.name) / "existing.json"
        path.write_text("previous result")
        with self.assertRaises(FileExistsError):
            idd.run_probe(self.k, SimpleNamespace(output=path))
        self.assertEqual(path.read_text(), "previous result")
        self.assertFalse(self.k.writes)


if __name__ == "__main__":
    unittest.main()

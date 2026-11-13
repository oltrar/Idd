"""Control-path tests with a simulated DRM kernel; these do not test GPU scanout."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("idd", Path(__file__).parents[1] / "src/idd.py")
idd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idd)


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


if __name__ == "__main__":
    unittest.main()

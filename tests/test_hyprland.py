"""Hyprland IPC/config tests; no compositor or GPU is required."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("idd_hyprland", Path(__file__).parents[1] / "src/idd_hyprland.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class FakeHyprctl:
    def __init__(self):
        self.monitor = {"name": "DP-1", "model": "Idd NVIDIA", "description": "IDD Idd NVIDIA GuestOnly-1",
                        "width": 1920, "height": 1080, "refreshRate": 60.0, "scale": 1,
                        "x": 0, "y": 0, "transform": 0, "disabled": False,
                        "availableModes": ["1920x1080@60.00Hz", "2560x1440@143.998Hz"]}
        self.commands = []
        self.fail_next_rule = False
        self.ignore_next_rule = False
        self.errors = []

    def monitors(self):
        self.commands.append(("monitors",))
        return [copy.deepcopy(self.monitor), {"name": "HDMI-A-1", "model": "Real monitor", "width": 2560}]

    def run(self, *args):
        self.commands.append(args)
        if args == ("configerrors",):
            return self.errors.pop(0) if self.errors else ""
        return "ok"

    def set_monitor(self, settings, disabled=False):
        self.commands.append(("rule", copy.deepcopy(settings), disabled))
        if self.fail_next_rule:
            self.fail_next_rule = False
            raise h.HyprlandError("mode rejected")
        if self.ignore_next_rule:
            self.ignore_next_rule = False
            return
        self.monitor["disabled"] = disabled
        if disabled:
            return
        if settings["mode"] != "preferred":
            w, rest = settings["mode"].split("x")
            height, refresh = rest.split("@")
            self.monitor.update(width=int(w), height=int(height), refreshRate=float(refresh))
        self.monitor["scale"] = settings["scale"]
        if settings["position"] != "auto":
            self.monitor["x"], self.monitor["y"] = (int(v) for v in settings["position"].split("x"))


class HyprlandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.integration = h.Integration(self.root)
        self.config = self.root / "hypr/hyprland.conf"
        self.config.parent.mkdir()
        self.original = "# My configuration\nmonitor = HDMI-A-1,preferred,0x0,1\n"
        self.config.write_text(self.original)
        self.settings = {"connector": "DP-1", "mode": "2560x1440@144", "position": "auto", "scale": 1,
                         "format": "hyprlang", "config": str(self.config)}
        self.client = FakeHyprctl()

    def test_detects_only_idd_output_and_checks_reported_rates(self):
        monitor = h.select_monitor(self.client.monitors())
        self.assertEqual(monitor["name"], "DP-1")
        h.check_mode(monitor, "2560x1440@144")
        with self.assertRaises(h.HyprlandError):
            h.check_mode(monitor, "2560x1440@240")
        with self.assertRaises(h.HyprlandError):
            h.select_monitor(self.client.monitors(), "HDMI-A-1")

    def test_multiple_idd_outputs_require_selection(self):
        another = dict(self.client.monitor, name="DP-2")
        with self.assertRaises(h.HyprlandError):
            h.select_monitor([self.client.monitor, another])
        self.assertEqual(h.select_monitor([self.client.monitor, another], "DP-2")["name"], "DP-2")

    def test_setup_persists_hyprlang_rule_and_preserves_other_output(self):
        result = self.integration.apply(self.client, self.settings, wait=0)
        self.assertEqual(result["width"], 2560)
        self.assertEqual(result["refreshRate"], 144)
        self.assertIn("monitor = HDMI-A-1,preferred,0x0,1", self.config.read_text())
        text = self.config.read_text()
        self.assertEqual(text.count("BEGIN IDD HYPRLAND"), 1)
        self.assertIn("source = ", text)
        include = self.integration.directory / "hyprland-monitor.conf"
        self.assertIn("monitor = DP-1,2560x1440@144,auto,1", include.read_text())
        self.assertEqual(self.integration.load(), self.settings)
        backups = list(self.config.parent.glob("*.idd-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)

    def test_apply_is_idempotent_without_duplicate_include(self):
        self.integration.apply(self.client, self.settings, wait=0)
        self.integration.apply(self.client, self.integration.load(), wait=0)
        self.assertEqual(self.config.read_text().count("BEGIN IDD HYPRLAND"), 1)
        self.assertEqual(len(list(self.config.parent.glob("*.idd-backup-*"))), 1)

    def test_lua_configuration_uses_native_monitor_and_require(self):
        self.config = self.config.with_suffix(".lua")
        self.config.write_text('hl.monitor({ output = "HDMI-A-1", mode = "preferred" })\n')
        settings = dict(self.settings, config=str(self.config), format="lua")
        self.integration.apply(self.client, settings, wait=0)
        self.assertIn("require(", self.config.read_text())
        self.assertIn("-- BEGIN IDD HYPRLAND", self.config.read_text())
        lua = (self.integration.directory / "hyprland-monitor.lua").read_text()
        self.assertIn('hl.monitor({ output = "DP-1", mode = "2560x1440@144"', lua)
        self.assertIn('hl.monitor({ output = "HDMI-A-1"', self.config.read_text())

    def test_unknown_mode_fails_before_editing_or_reloading(self):
        with self.assertRaises(h.HyprlandError):
            self.integration.apply(self.client, dict(self.settings, mode="3840x2160@480"), wait=0)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.integration.profile.exists())
        self.assertNotIn(("reload",), self.client.commands)

    def test_compositor_rejection_restores_files_and_previous_mode(self):
        self.client.fail_next_rule = True
        with self.assertRaisesRegex(h.HyprlandError, "previous files and monitor rule restored"):
            self.integration.apply(self.client, self.settings, wait=0)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.integration.profile.exists())
        self.assertFalse((self.integration.directory / "hyprland-monitor.conf").exists())
        self.assertEqual(self.client.monitor["width"], 1920)
        self.assertEqual(self.client.monitor["refreshRate"], 60)

    def test_ignored_rule_fails_verification_and_restores(self):
        self.client.ignore_next_rule = True
        with self.assertRaisesRegex(h.HyprlandError, "did not enable"):
            self.integration.apply(self.client, self.settings, wait=0)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(self.client.monitor["width"], 1920)

    def test_malformed_compositor_reply_also_restores_files(self):
        first = self.client.monitors()
        malformed = [dict(self.client.monitor, width=2560, height=1440, refreshRate=144, scale=None)]
        with mock.patch.object(self.client, "monitors", side_effect=[first, malformed]):
            with self.assertRaises(h.HyprlandError):
                self.integration.apply(self.client, self.settings, wait=0)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.integration.profile.exists())

    def test_root_does_not_enter_desktop_session(self):
        with mock.patch.object(h.sys, "platform", "linux"), \
             mock.patch.object(h.os, "geteuid", create=True, return_value=0), \
             mock.patch.object(h, "Hyprctl") as constructor:
            self.assertEqual(h.main(["status"]), 1)
        constructor.assert_not_called()

    def test_parse_errors_from_new_include_restore_main_config(self):
        self.client.errors = ["", "bad generated rule"]
        with self.assertRaisesRegex(h.HyprlandError, "config error"):
            self.integration.apply(self.client, self.settings, wait=0)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.integration.profile.exists())

    def test_existing_config_errors_do_not_modify_files(self):
        self.client.errors = ["existing invalid user setting"]
        with self.assertRaisesRegex(h.HyprlandError, "existing Hyprland config"):
            self.integration.apply(self.client, self.settings, wait=0)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.integration.profile.exists())

    def test_apply_reads_edited_profile(self):
        self.integration.apply(self.client, self.settings, wait=0)
        edited = dict(self.settings, mode="1920x1080@60", position="1920x0", scale=1.25)
        self.integration.profile.write_text(json.dumps(edited))
        actual = self.integration.apply(self.client, self.integration.load(), wait=0)
        self.assertEqual((actual["width"], actual["x"], actual["scale"]), (1920, 1920, 1.25))

    def test_remove_only_removes_managed_include(self):
        self.integration.apply(self.client, self.settings, wait=0)
        self.integration.remove(self.client)
        self.assertNotIn("IDD HYPRLAND", self.config.read_text())
        self.assertIn("monitor = HDMI-A-1,preferred,0x0,1", self.config.read_text())
        self.assertFalse(self.integration.profile.exists())

    def test_custom_config_and_ambiguous_default_detection(self):
        self.assertEqual(self.integration.config_path(), self.config.resolve())
        self.config.with_suffix(".lua").write_text("-- another configuration\n")
        with self.assertRaises(h.HyprlandError):
            self.integration.config_path()
        self.assertEqual(self.integration.config_path(self.config), self.config.resolve())

    def test_incomplete_managed_markers_do_not_overwrite_file(self):
        text = self.original + "# BEGIN IDD HYPRLAND\n"
        with self.assertRaises(h.HyprlandError):
            h.managed_config(text, "hyprlang", self.root / "monitor.conf")

    def test_settings_reject_injection_and_bad_values(self):
        bad = [{"connector": "DP-1;exec"}, {"mode": "1920x1080@60;exec touch /tmp/test"},
               {"position": "auto,mirror,HDMI-A-1"}, {"scale": float("nan")}, {"scale": True},
               {"format": "shell"}, {"transform": "0 });os.execute('bad')"}]
        for edit in bad:
            with self.subTest(edit=edit), self.assertRaises(h.HyprlandError):
                h.validate_settings(dict(self.settings, **edit))

    def test_ipc_uses_selected_instance_and_argument_list(self):
        client = h.Hyprctl("commit_123_456", executable="/usr/bin/hyprctl")
        response = subprocess.CompletedProcess([], 0, "ok\n", "")
        with mock.patch.object(h.subprocess, "run", return_value=response) as call:
            client.set_monitor(self.settings)
        self.assertEqual(call.call_args.args[0], ["/usr/bin/hyprctl", "-i", "commit_123_456", "keyword", "monitor", "DP-1,2560x1440@144,auto,1"])
        self.assertNotIn("shell", call.call_args.kwargs)

    def test_zero_exit_status_error_reply_is_not_success(self):
        client = h.Hyprctl("session", executable="hyprctl")
        response = subprocess.CompletedProcess([], 0, "Invalid monitor rule", "")
        with mock.patch.object(h.subprocess, "run", return_value=response):
            with self.assertRaises(h.HyprlandError):
                client.set_monitor(self.settings)

    def test_lua_ipc_and_refresh_tolerance(self):
        client = h.Hyprctl("session", executable="hyprctl")
        response = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(h.subprocess, "run", return_value=response) as call:
            client.set_monitor(dict(self.settings, format="lua"))
        self.assertEqual(call.call_args.args[0][3], "eval")
        self.client.monitor.update(width=2560, height=1440, refreshRate=143.998)
        self.assertEqual(h.verify_monitor(self.client, self.settings, wait=0)["width"], 2560)


if __name__ == "__main__":
    unittest.main()

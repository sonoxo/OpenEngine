#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from unittest import mock
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "Tools" / "Scripts" / "openengine.py"

spec = importlib.util.spec_from_file_location("openengine_build", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load Open Engine build orchestrator")
openengine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = openengine
spec.loader.exec_module(openengine)


class OpenEngineBuildToolTests(unittest.TestCase):
    def test_default_presets_cover_supported_desktop_hosts(self) -> None:
        self.assertEqual(
            openengine.default_preset("Windows", "AMD64"),
            "vs2026-x64-local",
        )
        self.assertEqual(
            openengine.default_preset("Windows", "AMD64", prefer_ninja=True),
            "ninja-x64-local",
        )
        self.assertEqual(
            openengine.default_preset("Windows", "ARM64"),
            "vs2026-arm64-local",
        )
        self.assertEqual(
            openengine.default_preset("Darwin", "arm64"),
            "macos-arm64-local",
        )
        self.assertEqual(
            openengine.default_preset("Darwin", "x86_64"),
            "macos-x64-local",
        )
        self.assertEqual(
            openengine.default_preset("Linux", "x86_64"),
            "linux-x64-local",
        )

    def test_windows_arm64_rejects_nonexistent_ninja_default(self) -> None:
        with self.assertRaises(openengine.BuildError):
            openengine.default_preset("Windows", "ARM64", prefer_ninja=True)

    def test_unknown_host_requires_explicit_preset(self) -> None:
        with self.assertRaises(openengine.BuildError):
            openengine.default_preset("Plan9", "mips")

    def test_single_config_configure_command_sets_build_type(self) -> None:
        command = openengine.make_configure_command(
            "linux-x64-local",
            {"generator": "Ninja"},
            configuration="Release",
            enable_tests=True,
            vcpkg_features="physics;nav;tests",
        )
        self.assertEqual(command[:3], ["cmake", "--preset", "linux-x64-local"])
        self.assertIn("-DCMAKE_BUILD_TYPE=Release", command)
        self.assertIn("-DBUILD_TESTING=ON", command)
        self.assertIn(
            "-DVCPKG_MANIFEST_FEATURES=physics;nav;tests",
            command,
        )

    def test_multi_config_configure_command_does_not_set_build_type(self) -> None:
        command = openengine.make_configure_command(
            "vs2026-x64-local",
            {"generator": "Visual Studio 18 2026"},
            configuration="DebugFast",
            enable_tests=False,
            vcpkg_features=None,
        )
        self.assertNotIn("-DCMAKE_BUILD_TYPE=DebugFast", command)

    def test_build_command_deduplicates_targets_and_uses_configuration(self) -> None:
        command = openengine.make_build_command(
            "macos-arm64-local",
            {"generator": "Xcode"},
            configuration="DebugFast",
            targets=["Editor", "McpServer", "Editor"],
            jobs=8,
        )
        self.assertEqual(
            command,
            [
                "cmake",
                "--build",
                "--preset",
                "macos-arm64-local",
                "--config",
                "DebugFast",
                "--target",
                "Editor",
                "McpServer",
                "-j",
                "8",
            ],
        )

    def test_single_config_build_command_omits_config_switch(self) -> None:
        command = openengine.make_build_command(
            "linux-x64-local",
            {"generator": "Ninja"},
            configuration="DebugFast",
            targets=["Editor"],
            jobs=4,
        )
        self.assertNotIn("--config", command)

    def test_preset_loader_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "CMakePresets.json"
            path.write_text("{ invalid", encoding="utf-8")
            with self.assertRaises(openengine.BuildError):
                openengine.load_presets(path)

    def test_repository_preset_file_contains_orchestrator_defaults(self) -> None:
        presets = openengine.configure_presets(openengine.load_presets())
        expected = {
            "vs2026-x64-local",
            "vs2026-arm64-local",
            "ninja-x64-local",
            "macos-arm64-local",
            "macos-x64-local",
            "linux-x64-local",
        }
        self.assertTrue(expected.issubset(presets))

    def test_plan_cli_is_deterministic(self) -> None:
        parser = openengine.build_parser()
        args = parser.parse_args(
            [
                "plan",
                "--preset",
                "linux-x64-local",
                "--config",
                "DebugFast",
                "--target",
                "Editor",
                "--with-mcp",
                "--tests",
                "--jobs",
                "6",
            ]
        )
        preset_name, preset, configuration, commands = openengine.plan_commands(args)
        self.assertEqual(preset_name, "linux-x64-local")
        self.assertEqual(configuration, "DebugFast")
        self.assertEqual(preset["generator"], "Ninja")
        self.assertEqual(
            commands[0],
            [
                "cmake",
                "--preset",
                "linux-x64-local",
                "-DCMAKE_BUILD_TYPE=DebugFast",
                "-DBUILD_TESTING=ON",
            ],
        )
        self.assertEqual(
            commands[1],
            [
                "cmake",
                "--build",
                "--preset",
                "linux-x64-local",
                "--target",
                "Editor",
                "McpServer",
                "-j",
                "6",
            ],
        )


    def test_sanitizer_preset_preserves_debug_configuration(self) -> None:
        data = openengine.load_presets()
        preset_name, preset = openengine.resolve_preset(
            "macos-arm64-local-asan",
            data=data,
        )
        self.assertEqual(preset["generator"], "Ninja")
        self.assertEqual(
            openengine.resolve_configuration(None, preset_name, preset, data),
            "Debug",
        )

    def test_configure_only_preset_builds_by_binary_directory(self) -> None:
        data = openengine.load_presets()
        preset_name, preset = openengine.resolve_preset("vs2026-x64", data=data)
        command = openengine.make_build_command(
            preset_name,
            preset,
            configuration="DebugFast",
            targets=["Editor"],
            jobs=4,
            use_build_preset=False,
        )
        self.assertEqual(
            command[:3],
            ["cmake", "--build", "build/vs2026-x64"],
        )
        self.assertIn("--config", command)
        self.assertIn("DebugFast", command)

    def test_plan_uses_configure_only_preset_without_build_preset(self) -> None:
        parser = openengine.build_parser()
        args = parser.parse_args(
            [
                "plan",
                "--preset",
                "vs2026-x64",
                "--target",
                "Editor",
                "--jobs",
                "4",
            ]
        )
        preset_name, _, configuration, commands = openengine.plan_commands(args)
        self.assertEqual(preset_name, "vs2026-x64")
        self.assertEqual(configuration, "DebugFast")
        self.assertEqual(
            commands[1][:3],
            ["cmake", "--build", "build/vs2026-x64"],
        )

    def test_json_preset_file_is_valid_json(self) -> None:
        json.loads((ROOT / "CMakePresets.json").read_text(encoding="utf-8"))


class OpenEngineBuildToolRegressionTests(unittest.TestCase):
    def test_steamdeck_defaults_to_player(self) -> None:
        data = openengine.load_presets()
        preset_name, preset = openengine.resolve_preset(
            "steamdeck-x64-local",
            data=data,
        )
        self.assertEqual(
            openengine.default_targets(preset_name, preset, data),
            ["Player"],
        )

    def test_wasm_defaults_to_web_editor(self) -> None:
        data = openengine.load_presets()
        preset_name, preset = openengine.resolve_preset("wasm-debug", data=data)
        self.assertEqual(
            openengine.default_targets(preset_name, preset, data),
            ["WebEditor"],
        )

    def test_build_preset_targets_take_precedence(self) -> None:
        data = openengine.load_presets()
        preset_name, preset = openengine.resolve_preset(
            "macos-arm64-editor-fast",
            data=data,
        )
        self.assertEqual(
            openengine.default_targets(preset_name, preset, data),
            ["Editor"],
        )

    def test_single_config_rejects_config_override_with_no_configure(self) -> None:
        data = openengine.load_presets()
        preset_name, preset = openengine.resolve_preset(
            "linux-x64-local",
            data=data,
        )
        with self.assertRaises(openengine.BuildError):
            openengine.validate_no_configure_configuration(
                no_configure=True,
                requested_configuration="Release",
                preset_name=preset_name,
                preset=preset,
            )

    def test_multi_config_allows_config_override_with_no_configure(self) -> None:
        data = openengine.load_presets()
        preset_name, preset = openengine.resolve_preset(
            "vs2026-x64-local",
            data=data,
        )
        openengine.validate_no_configure_configuration(
            no_configure=True,
            requested_configuration="Release",
            preset_name=preset_name,
            preset=preset,
        )

    def test_mcp_validation_rejects_old_node(self) -> None:
        with mock.patch.object(
            openengine,
            "first_output",
            side_effect=[(True, "v18.20.0"), (True, "10.0.0")],
        ):
            with self.assertRaises(openengine.BuildError):
                openengine.validate_mcp_toolchain()

    def test_mcp_validation_requires_npm(self) -> None:
        with mock.patch.object(
            openengine,
            "first_output",
            side_effect=[(True, "v20.12.0"), (False, "not found in PATH")],
        ):
            with self.assertRaises(openengine.BuildError):
                openengine.validate_mcp_toolchain()

    def test_mcp_validation_accepts_node20_and_npm(self) -> None:
        with mock.patch.object(
            openengine,
            "first_output",
            side_effect=[(True, "v20.12.0"), (True, "10.5.0")],
        ):
            openengine.validate_mcp_toolchain()


if __name__ == "__main__":
    unittest.main(verbosity=2)

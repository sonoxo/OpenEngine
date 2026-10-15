#!/usr/bin/env python3
"""Cross-platform Open Engine build orchestrator.

This is a thin command layer over the repository's CMake presets. It owns no
compiler logic and does not duplicate the engine build graph.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESETS_PATH = REPO_ROOT / "CMakePresets.json"
VERSION_PATH = REPO_ROOT / "VERSION"
MIN_CMAKE = (3, 25, 0)
MIN_PYTHON = (3, 10)
MIN_NODE = (20, 0, 0)


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolStatus:
    name: str
    required: bool
    available: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "available": self.available,
            "detail": self.detail,
        }


def load_presets(path: Path = PRESETS_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildError(f"CMake preset file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BuildError(f"Invalid CMake preset JSON: {exc}") from exc


def configure_presets(data: dict) -> dict[str, dict]:
    return {
        preset["name"]: preset
        for preset in data.get("configurePresets", [])
        if isinstance(preset, dict) and isinstance(preset.get("name"), str)
    }


def build_presets(data: dict) -> dict[str, dict]:
    return {
        preset["name"]: preset
        for preset in data.get("buildPresets", [])
        if isinstance(preset, dict) and isinstance(preset.get("name"), str)
    }


def merge_preset_values(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_preset_values(merged[key], value)
        else:
            merged[key] = value
    return merged


def materialize_configure_preset(
    name: str,
    presets: dict[str, dict],
    stack: tuple[str, ...] = (),
) -> dict:
    if name in stack:
        chain = " -> ".join((*stack, name))
        raise BuildError(f"CMake preset inheritance cycle: {chain}")
    raw = presets.get(name)
    if raw is None:
        raise BuildError(f"Unknown inherited configure preset: {name}")

    parents = raw.get("inherits", [])
    if isinstance(parents, str):
        parents = [parents]

    merged: dict = {}
    for parent in parents:
        if not isinstance(parent, str):
            raise BuildError(f"Invalid inherited preset name in {name}")
        merged = merge_preset_values(
            merged,
            materialize_configure_preset(parent, presets, (*stack, name)),
        )
    return merge_preset_values(merged, raw)


def canonical_machine(machine: str) -> str:
    value = machine.strip().lower()
    if value in {"amd64", "x86_64", "x64"}:
        return "x64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value


def default_preset(system: str, machine: str, prefer_ninja: bool = False) -> str:
    system_name = system.strip().lower()
    arch = canonical_machine(machine)

    if system_name == "windows":
        if arch == "arm64":
            if prefer_ninja:
                raise BuildError("No Windows ARM64 Ninja preset exists; use vs2026-arm64-local.")
            return "vs2026-arm64-local"
        if arch == "x64":
            return "ninja-x64-local" if prefer_ninja else "vs2026-x64-local"

    if system_name == "darwin":
        if arch == "arm64":
            return "macos-arm64-local"
        if arch == "x64":
            return "macos-x64-local"

    if system_name == "linux" and arch == "x64":
        return "linux-x64-local"

    raise BuildError(
        f"No default preset for {system}/{machine}. "
        "Pass --preset with one of: python Tools/Scripts/openengine.py presets"
    )


def resolve_preset(
    requested: str | None,
    *,
    system: str | None = None,
    machine: str | None = None,
    prefer_ninja: bool = False,
    data: dict | None = None,
) -> tuple[str, dict]:
    preset_data = data or load_presets()
    presets = configure_presets(preset_data)
    name = requested or default_preset(
        system or platform.system(),
        machine or platform.machine(),
        prefer_ninja,
    )
    if name not in presets:
        raise BuildError(f"Unknown configure preset: {name}")
    return name, materialize_configure_preset(name, presets)


def resolve_configuration(
    requested: str | None,
    preset_name: str,
    preset: dict,
    data: dict,
) -> str:
    if requested:
        return requested

    build_preset = build_presets(data).get(preset_name, {})
    build_configuration = build_preset.get("configuration")
    if isinstance(build_configuration, str) and build_configuration:
        return build_configuration

    cache_variables = preset.get("cacheVariables", {})
    configured_type = (
        cache_variables.get("CMAKE_BUILD_TYPE")
        if isinstance(cache_variables, dict)
        else None
    )
    if isinstance(configured_type, str) and configured_type:
        return configured_type

    return "DebugFast"


def expand_binary_dir(preset_name: str, preset: dict) -> str:
    raw = preset.get("binaryDir")
    if not isinstance(raw, str) or not raw:
        raise BuildError(
            f"Configure preset {preset_name} has no build preset or binaryDir."
        )
    if "$env{" in raw or "$penv{" in raw:
        raise BuildError(
            f"Configure preset {preset_name} uses an environment macro in binaryDir; "
            "add a matching build preset."
        )

    replacements = {
        "${sourceDir}": str(REPO_ROOT),
        "${sourceParentDir}": str(REPO_ROOT.parent),
        "${sourceDirName}": REPO_ROOT.name,
        "${presetName}": preset_name,
    }
    result = raw
    for marker, value in replacements.items():
        result = result.replace(marker, value)
    return result


def is_multi_config(preset: dict) -> bool:
    generator = str(preset.get("generator", ""))
    return (
        generator.startswith("Visual Studio")
        or generator == "Xcode"
        or "Multi-Config" in generator
    )


def make_configure_command(
    preset_name: str,
    preset: dict,
    *,
    configuration: str,
    enable_tests: bool,
    vcpkg_features: str | None,
) -> list[str]:
    command = ["cmake", "--preset", preset_name]
    if not is_multi_config(preset):
        command.append(f"-DCMAKE_BUILD_TYPE={configuration}")
    if enable_tests:
        command.append("-DBUILD_TESTING=ON")
    if vcpkg_features:
        command.append(f"-DVCPKG_MANIFEST_FEATURES={vcpkg_features}")
    return command


def make_build_command(
    preset_name: str,
    preset: dict,
    *,
    configuration: str,
    targets: Iterable[str],
    jobs: int,
    use_build_preset: bool = True,
) -> list[str]:
    if use_build_preset:
        command = ["cmake", "--build", "--preset", preset_name]
    else:
        command = ["cmake", "--build", expand_binary_dir(preset_name, preset)]
    if is_multi_config(preset):
        command.extend(["--config", configuration])

    target_list = list(dict.fromkeys(targets))
    if target_list:
        command.extend(["--target", *target_list])
    command.extend(["-j", str(jobs)])
    return command


def command_text(command: Iterable[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def run_command(command: list[str], *, dry_run: bool) -> None:
    print(f"+ {command_text(command)}", flush=True)
    if dry_run:
        return
    subprocess.run(command, cwd=REPO_ROOT, check=True)


def parse_version(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in value.strip().split("."):
        digits = ""
        for char in token:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def first_output(command: list[str]) -> tuple[bool, str]:
    executable = shutil.which(command[0])
    if not executable:
        return False, "not found in PATH"
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    combined = (result.stdout or result.stderr).strip().splitlines()
    detail = combined[0].strip() if combined else f"exit {result.returncode}"
    return result.returncode == 0, detail


def tool_statuses(preset: dict) -> list[ToolStatus]:
    generator = str(preset.get("generator", ""))
    statuses: list[ToolStatus] = []

    cmake_ok, cmake_detail = first_output(["cmake", "--version"])
    if cmake_ok:
        version_words = cmake_detail.split()
        version = parse_version(version_words[-1]) if version_words else ()
        cmake_ok = version >= MIN_CMAKE
        if not cmake_ok:
            cmake_detail = f"{cmake_detail}; need >= {'.'.join(map(str, MIN_CMAKE))}"
    statuses.append(ToolStatus("cmake", True, cmake_ok, cmake_detail))

    git_ok, git_detail = first_output(["git", "--version"])
    statuses.append(ToolStatus("git", True, git_ok, git_detail))

    dotnet_ok, dotnet_detail = first_output(["dotnet", "--list-sdks"])
    if dotnet_ok:
        try:
            result = subprocess.run(
                ["dotnet", "--list-sdks"],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            sdk_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            has_dotnet_10 = any(line.startswith("10.") for line in sdk_lines)
            dotnet_ok = has_dotnet_10
            dotnet_detail = sdk_lines[-1] if sdk_lines else "no SDKs listed"
            if not has_dotnet_10:
                dotnet_detail += "; .NET 10 SDK required"
        except (OSError, subprocess.SubprocessError) as exc:
            dotnet_ok = False
            dotnet_detail = str(exc)
    statuses.append(ToolStatus("dotnet", True, dotnet_ok, dotnet_detail))

    if "Ninja" in generator:
        ninja_ok, ninja_detail = first_output(["ninja", "--version"])
        statuses.append(ToolStatus("ninja", True, ninja_ok, ninja_detail))

    system_name = platform.system()
    if system_name == "Linux":
        compiler = "c++" if shutil.which("c++") else "g++"
        compiler_ok, compiler_detail = first_output([compiler, "--version"])
        statuses.append(ToolStatus("c++", True, compiler_ok, compiler_detail))
    elif system_name == "Darwin":
        xcode_ok, xcode_detail = first_output(["xcode-select", "-p"])
        statuses.append(ToolStatus("xcode", True, xcode_ok, xcode_detail))
    elif system_name == "Windows":
        if "Ninja" in generator:
            compiler_ok, compiler_detail = first_output(["cl", "/?"])
            if not compiler_ok and shutil.which("clang-cl"):
                compiler_ok, compiler_detail = first_output(["clang-cl", "--version"])
            statuses.append(ToolStatus("compiler", True, compiler_ok, compiler_detail))
        elif generator.startswith("Visual Studio"):
            compiler_ok, compiler_detail = first_output(["cl", "/?"])
            statuses.append(ToolStatus("msvc", True, compiler_ok, compiler_detail))

    node_ok, node_detail = first_output(["node", "--version"])
    statuses.append(ToolStatus("node", False, node_ok, node_detail))

    python_ok = sys.version_info >= MIN_PYTHON
    python_detail = platform.python_version()
    statuses.append(ToolStatus("python", False, python_ok, python_detail))
    return statuses


def doctor_report(preset_name: str, preset: dict) -> dict[str, object]:
    statuses = tool_statuses(preset)
    warnings: list[str] = []

    if platform.system() == "Windows" and " " in str(REPO_ROOT):
        warnings.append("Windows clone paths containing spaces are unsupported by several third-party builds.")

    if not (REPO_ROOT / "dependencies" / "vcpkg").exists():
        warnings.append("vcpkg is not bootstrapped yet; the first configure will provision it.")

    return {
        "engine_version": VERSION_PATH.read_text(encoding="utf-8").strip(),
        "repository": str(REPO_ROOT),
        "system": platform.system(),
        "machine": platform.machine(),
        "preset": preset_name,
        "generator": preset.get("generator", ""),
        "tools": [status.as_dict() for status in statuses],
        "warnings": warnings,
        "ready": all(status.available for status in statuses if status.required),
    }


def print_doctor(report: dict[str, object]) -> None:
    print(f"Open Engine {report['engine_version']}")
    print(f"Preset: {report['preset']} ({report['generator']})")
    for raw in report["tools"]:
        status = raw
        marker = "OK" if status["available"] else ("WARN" if not status["required"] else "MISSING")
        requirement = "required" if status["required"] else "optional"
        print(f"[{marker:7}] {status['name']:8} {requirement:8} {status['detail']}")
    for warning in report["warnings"]:
        print(f"[WARN   ] {warning}")
    print("READY" if report["ready"] else "NOT READY")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openengine",
        description="Cross-platform Open Engine build orchestrator.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    presets_parser = subparsers.add_parser("presets", help="List configure presets.")
    presets_parser.add_argument("--json", action="store_true")

    doctor = subparsers.add_parser("doctor", help="Check the selected build environment.")
    doctor.add_argument("--preset")
    doctor.add_argument("--ninja", action="store_true", help="Prefer the Windows Ninja preset.")
    doctor.add_argument("--json", action="store_true")

    for command_name, help_text in (
        ("plan", "Print the configure/build commands without running them."),
        ("build", "Configure and build one or more targets."),
    ):
        command = subparsers.add_parser(command_name, help=help_text)
        command.add_argument("--preset")
        command.add_argument("--ninja", action="store_true", help="Prefer the Windows Ninja preset.")
        command.add_argument(
            "--config",
            default=None,
            choices=("DebugFast", "Debug", "RelWithDebInfo", "Release", "MinSizeRel"),
            help="Override the preset configuration; otherwise use the preset value or DebugFast.",
        )
        command.add_argument("--target", action="append", dest="targets")
        command.add_argument("--with-mcp", action="store_true")
        command.add_argument("--tests", action="store_true", help="Configure BUILD_TESTING=ON.")
        command.add_argument("--vcpkg-features")
        command.add_argument("--jobs", type=int, default=max(1, min(12, os.cpu_count() or 1)))
        command.add_argument("--no-configure", action="store_true")
        if command_name == "build":
            command.add_argument("--dry-run", action="store_true")

    return parser


def default_targets(
    preset_name: str,
    preset: dict,
    data: dict,
) -> list[str]:
    build_preset = build_presets(data).get(preset_name, {})
    explicit_targets = build_preset.get("targets")
    if isinstance(explicit_targets, list) and all(
        isinstance(target, str) and target for target in explicit_targets
    ):
        return list(explicit_targets)

    cache_variables = preset.get("cacheVariables", {})
    if not isinstance(cache_variables, dict):
        cache_variables = {}

    if str(cache_variables.get("GE_WASM", "")).upper() == "ON":
        return ["WebEditor"]

    editor_enabled = str(cache_variables.get("BUILD_EDITOR", "ON")).upper() != "OFF"
    player_enabled = str(cache_variables.get("BUILD_PLAYER", "ON")).upper() != "OFF"
    if not editor_enabled and player_enabled:
        return ["Player"]
    if editor_enabled:
        return ["Editor"]
    if player_enabled:
        return ["Player"]

    raise BuildError(
        f"Configure preset {preset_name} disables Editor and Player; pass --target explicitly."
    )


def normalized_targets(
    raw_targets: list[str] | None,
    with_mcp: bool,
    *,
    preset_name: str,
    preset: dict,
    data: dict,
) -> list[str]:
    targets = list(raw_targets or default_targets(preset_name, preset, data))
    if with_mcp and "McpServer" not in targets:
        targets.append("McpServer")
    return targets


def validate_no_configure_configuration(
    *,
    no_configure: bool,
    requested_configuration: str | None,
    preset_name: str,
    preset: dict,
) -> None:
    if no_configure and requested_configuration is not None and not is_multi_config(preset):
        raise BuildError(
            f"--config cannot be combined with --no-configure for single-config preset "
            f"{preset_name}; run configure so CMAKE_BUILD_TYPE is updated."
        )


def validate_mcp_toolchain() -> None:
    node_ok, node_detail = first_output(["node", "--version"])
    if not node_ok:
        raise BuildError("--with-mcp requires Node.js 20 or newer on PATH.")
    node_version = parse_version(node_detail.lstrip("vV"))
    if node_version < MIN_NODE:
        raise BuildError(
            f"--with-mcp requires Node.js >= {'.'.join(map(str, MIN_NODE))}; "
            f"found {node_detail}."
        )

    npm_ok, npm_detail = first_output(["npm", "--version"])
    if not npm_ok:
        raise BuildError(f"--with-mcp requires npm on PATH ({npm_detail}).")


def validate_jobs(jobs: int) -> None:
    if jobs < 1 or jobs > 256:
        raise BuildError("--jobs must be between 1 and 256")


def plan_commands(args: argparse.Namespace) -> tuple[str, dict, str, list[list[str]]]:
    validate_jobs(args.jobs)
    preset_data = load_presets()
    preset_name, preset = resolve_preset(
        args.preset,
        prefer_ninja=args.ninja,
        data=preset_data,
    )
    configuration = resolve_configuration(
        args.config,
        preset_name,
        preset,
        preset_data,
    )
    validate_no_configure_configuration(
        no_configure=args.no_configure,
        requested_configuration=args.config,
        preset_name=preset_name,
        preset=preset,
    )
    targets = normalized_targets(
        args.targets,
        args.with_mcp,
        preset_name=preset_name,
        preset=preset,
        data=preset_data,
    )

    commands: list[list[str]] = []
    if not args.no_configure:
        commands.append(
            make_configure_command(
                preset_name,
                preset,
                configuration=configuration,
                enable_tests=args.tests,
                vcpkg_features=args.vcpkg_features,
            )
        )
    commands.append(
        make_build_command(
            preset_name,
            preset,
            configuration=configuration,
            targets=targets,
            jobs=args.jobs,
            use_build_preset=preset_name in build_presets(preset_data),
        )
    )
    return preset_name, preset, configuration, commands


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "presets":
            names = sorted(configure_presets(load_presets()))
            if args.json:
                print(json.dumps(names, indent=2))
            else:
                print("\n".join(names))
            return 0

        if args.command == "doctor":
            preset_name, preset = resolve_preset(args.preset, prefer_ninja=args.ninja)
            report = doctor_report(preset_name, preset)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print_doctor(report)
            return 0 if report["ready"] else 1

        preset_name, preset, configuration, commands = plan_commands(args)
        print(f"Open Engine preset: {preset_name} ({preset.get('generator', '')})")
        print(f"Configuration: {configuration}")

        if args.command == "plan":
            for command in commands:
                print(command_text(command))
            return 0

        if args.with_mcp and not args.dry_run:
            validate_mcp_toolchain()

        for command in commands:
            run_command(command, dry_run=args.dry_run)
        return 0

    except BuildError as exc:
        print(f"openengine: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"openengine: command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())

import tempfile
import unittest

from pathlib import Path

from scripts.validate_observability_migration import collect_violations


class ObservabilityMigrationValidationTests(unittest.TestCase):
    def test_repository_has_no_legacy_observability_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]

        violations = collect_violations(root)

        self.assertEqual([violation.render() for violation in violations], [])

    def test_detects_direct_process_print_and_legacy_native_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            python_root = root / "bd_to_avp"
            swift_root = root / "macos" / "BluRayToVisionPro"
            python_root.mkdir()
            swift_root.mkdir(parents=True)
            (python_root / "rogue.py").write_text(
                "import atexit\n"
                "import builtins\n"
                "import subprocess\n"
                "import sys\n\n"
                "def kill_child_processes():\n"
                "    builtins.print('tool output')\n"
                "    sys.stdout.write('raw output')\n"
                "    subprocess.run(['tool'])\n\n"
                "atexit.register(kill_child_processes)\n",
                encoding="utf-8",
            )
            (swift_root / "Rogue.swift").write_text(
                'struct Rogue { var diagnosticLog = "" }\n',
                encoding="utf-8",
            )

            violations = collect_violations(root)

        self.assertEqual(
            {violation.code for violation in violations},
            {
                "direct-process-call",
                "global-cleanup-hook",
                "legacy-symbol",
                "non-presentation-output",
            },
        )

    def test_detects_process_aliases_posix_spawns_and_dunder_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            python_root = root / "bd_to_avp"
            (root / "macos" / "BluRayToVisionPro").mkdir(parents=True)
            python_root.mkdir()
            (python_root / "rogue.py").write_text(
                "import os\n"
                "import ffmpeg\n"
                "import subprocess\n"
                "import sys\n\n"
                "launch = subprocess.run\n\n"
                "def execute():\n"
                "    sys.__stdout__.write('raw output')\n"
                "    launch(['tool'])\n"
                "    os.posix_spawn('/usr/bin/true', ['true'], {})\n"
                "    ffmpeg.probe('source.mkv')\n"
                "    os.write(2, b'raw bytes')\n",
                encoding="utf-8",
            )

            violations = collect_violations(root)

        self.assertEqual(
            [violation.code for violation in violations].count("direct-process-call"),
            3,
        )
        self.assertEqual(
            [violation.code for violation in violations].count("non-presentation-output"),
            2,
        )


if __name__ == "__main__":
    unittest.main()

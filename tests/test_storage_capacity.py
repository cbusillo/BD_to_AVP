import os
import threading
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bd_to_avp.modules.storage_capacity import probe_storage_capacity


class StorageCapacityProbeTests(unittest.TestCase):
    def test_known_capacity_reports_posix_provenance(self) -> None:
        values = SimpleNamespace(
            f_bavail=17 * 1024,
            f_frsize=1024**3,
            f_blocks=20 * 1024,
            f_flag=0,
        )
        with (
            patch("bd_to_avp.modules.storage_capacity.os.statvfs", return_value=values),
            patch("bd_to_avp.modules.storage_capacity.os.access", return_value=True),
        ):
            storage = probe_storage_capacity(
                Path("/Volumes/mock-share/workspace"),
                role="preview_workspace",
                required_bytes=10 * 1024**3,
            )

        self.assertEqual(storage.capacity_state, "known")
        self.assertEqual(storage.capacity_sufficiency, "sufficient")
        self.assertEqual(storage.capacity_provenance, ("worker_posix_statvfs",))
        self.assertGreater(storage.available_bytes or 0, 16 * 1024**4)

    def test_zero_on_writable_volume_is_unknown(self) -> None:
        values = SimpleNamespace(f_bavail=0, f_frsize=4096, f_blocks=100, f_flag=0)
        with (
            patch("bd_to_avp.modules.storage_capacity.os.statvfs", return_value=values),
            patch("bd_to_avp.modules.storage_capacity.os.access", return_value=True),
        ):
            storage = probe_storage_capacity(
                Path("/Volumes/mock-share"),
                role="destination",
            )

        self.assertEqual(storage.capacity_state, "unknown")
        self.assertEqual(storage.capacity_unknown_reason, "zero_on_writable_volume")
        self.assertIsNone(storage.available_bytes)

    def test_read_only_workspace_keeps_capacity_sufficiency_unknown(self) -> None:
        values = SimpleNamespace(
            f_bavail=100,
            f_frsize=1024**3,
            f_blocks=200,
            f_flag=getattr(os, "ST_RDONLY", 1),
        )
        with (
            patch("bd_to_avp.modules.storage_capacity.os.statvfs", return_value=values),
            patch("bd_to_avp.modules.storage_capacity.os.access", return_value=False),
        ):
            storage = probe_storage_capacity(
                Path("/Volumes/mock-share/temp_files"),
                role="partial_workspace",
                required_bytes=10 * 1024**3,
            )

        self.assertEqual(storage.capacity_state, "known")
        self.assertEqual(storage.capacity_sufficiency, "sufficient")
        self.assertTrue(storage.read_only)

    def test_permission_failure_is_unknown(self) -> None:
        with patch(
            "bd_to_avp.modules.storage_capacity.os.statvfs",
            side_effect=PermissionError("denied"),
        ):
            storage = probe_storage_capacity(
                Path("/Volumes/mock-share"),
                role="destination",
            )

        self.assertEqual(storage.status, "inaccessible")
        self.assertEqual(storage.capacity_state, "unknown")
        self.assertEqual(storage.capacity_unknown_reason, "permission_denied")

    def test_missing_workspace_probes_nearest_existing_parent(self) -> None:
        values = SimpleNamespace(f_bavail=20, f_frsize=1024**3, f_blocks=40, f_flag=0)
        calls = 0

        def statvfs(_path: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FileNotFoundError
            return values

        with (
            patch("bd_to_avp.modules.storage_capacity.os.statvfs", side_effect=statvfs),
            patch("bd_to_avp.modules.storage_capacity.os.access", return_value=True),
        ):
            storage = probe_storage_capacity(
                Path("/Volumes/mock-share/missing/temp_files"),
                role="partial_workspace",
            )

        self.assertEqual(storage.capacity_state, "known")
        self.assertEqual(calls, 2)

    def test_stalled_probe_times_out_without_blocking(self) -> None:
        release = threading.Event()

        def stalled(_path: object) -> object:
            release.wait(1)
            return SimpleNamespace(f_bavail=1, f_frsize=1, f_blocks=1, f_flag=0)

        with patch("bd_to_avp.modules.storage_capacity.os.statvfs", side_effect=stalled):
            storage = probe_storage_capacity(
                Path("/Volumes/mock-share"),
                role="destination",
                timeout_seconds=0.01,
            )
        release.set()

        self.assertEqual(storage.capacity_state, "unknown")
        self.assertEqual(storage.capacity_unknown_reason, "timeout")


if __name__ == "__main__":
    unittest.main()

import copy
import json
import tempfile
import unittest

from pathlib import Path

from scripts.validate_video_quality_route_table import (
    DEFAULT_ROUTE_TABLE,
    RouteTableError,
    validate_route_table,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VideoQualityRouteTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_ROUTE_TABLE.read_text(encoding="utf-8"))

    def _validate(self, document: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "route-table.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return validate_route_table(path, repository_root=REPOSITORY_ROOT)

    @staticmethod
    def _route(document: dict[str, object], route_id: str) -> dict[str, object]:
        return next(route for route in document["routes"] if route["id"] == route_id)

    @staticmethod
    def _mapping(route: dict[str, object], step_id: str) -> dict[str, object]:
        return next(mapping for mapping in route["mappings"] if mapping["step_id"] == step_id)

    def test_committed_candidate_table_is_complete_but_pending_qualification(self) -> None:
        receipt = validate_route_table()

        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["mapping_version"], 2)
        self.assertEqual(receipt["status"], "candidate_pending_qualification")
        self.assertEqual(receipt["qualification_issue"], "#422")
        self.assertEqual(len(receipt["supported_step_ids"]["direct_mv_hevc"]), 7)
        self.assertEqual(receipt["supported_step_ids"]["generated_mv_hevc"], ["balanced"])
        self.assertEqual(receipt["supported_step_ids"]["upscale_quality"], ["balanced", "detailed"])
        self.assertEqual(receipt["supported_step_ids"]["av1_sbs"], [])

    def test_rejects_generated_balanced_alias_for_unsupported_step(self) -> None:
        document = copy.deepcopy(self.document)
        generated = self._route(document, "generated_mv_hevc")
        detailed = self._mapping(generated, "detailed")
        detailed.update(status="candidate", values={"eye_bitrate_mbps": 20, "merge_quality": 75})

        with self.assertRaisesRegex(RouteTableError, "must remain unavailable without an alias"):
            self._validate(document)

    def test_rejects_changed_direct_mapping(self) -> None:
        document = copy.deepcopy(self.document)
        direct = self._route(document, "direct_mv_hevc")
        self._mapping(direct, "maximum_detail")["values"] = {"quality": 0.84}

        with self.assertRaisesRegex(RouteTableError, "frozen candidate mapping"):
            self._validate(document)

    def test_rejects_balanced_fallback_expansion(self) -> None:
        document = copy.deepcopy(self.document)
        document["fallbacks"][0]["supported_step_ids"].append("detailed")

        with self.assertRaisesRegex(RouteTableError, "Balanced-only"):
            self._validate(document)

    def test_rejects_release_qualification_claim(self) -> None:
        document = copy.deepcopy(self.document)
        document["status"] = "qualified"

        with self.assertRaisesRegex(RouteTableError, "pending #422"):
            self._validate(document)

    def test_rejects_changed_evidence_binding(self) -> None:
        document = copy.deepcopy(self.document)
        document["evidence_receipts"]["ordinary_direct_confirmation"]["plan"]["sha256"] = "0" * 64

        with self.assertRaisesRegex(RouteTableError, "does not match the referenced file"):
            self._validate(document)


if __name__ == "__main__":
    unittest.main()

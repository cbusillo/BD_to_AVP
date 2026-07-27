import unittest

from bd_to_avp.modules.aac_layout_policy import identity_map
from scripts import qualify_apple_aac_layouts as qualification


class AppleAacLayoutQualificationTests(unittest.TestCase):
    def test_matrix_covers_standard_layouts_from_one_through_eight_channels(self) -> None:
        layouts = {case.source_layout for case in qualification.LAYOUT_CASES}

        self.assertEqual(
            {len(case.source_channels) for case in qualification.LAYOUT_CASES},
            set(range(1, 9)),
        )
        self.assertEqual(layouts, set(qualification.LAYOUT_CHANNELS))
        self.assertTrue(
            {
                "5.0(side)",
                "5.1(side)",
                "6.1",
                "7.1(wide)",
                "7.1(wide-side)",
            }.issubset(layouts)
        )

    def test_policy_rows_are_explicit_and_account_for_every_source_channel(self) -> None:
        actions = {case.action for case in qualification.LAYOUT_CASES}

        self.assertEqual(actions, {"preserve", "remap", "downmix"})
        self.assertEqual(qualification.UNLISTED_LAYOUT_POLICY["action"], "fail")
        for case in qualification.LAYOUT_CASES:
            with self.subTest(layout=case.source_layout):
                self.assertEqual(set(case.expected_identity_map), set(case.source_channels))
                self.assertIn(case.target_layout, qualification.CANONICAL_AAC_CONFIGURATION)
                self.assertTrue(
                    all(
                        output
                        in qualification.AAC_CHANNEL_CONFIGURATION_LAYOUTS[
                            qualification.CANONICAL_AAC_CONFIGURATION[case.target_layout]
                        ]
                        for outputs in case.expected_identity_map.values()
                        for output in outputs
                    )
                )
                if case.action == "preserve":
                    self.assertIsNone(case.pan_filter)
                    self.assertEqual(case.source_layout, case.target_layout)
                else:
                    self.assertIsNotNone(case.pan_filter)

        self.assertFalse(qualification.runtime_behavior_changed())

    def test_aac_channel_configuration_is_read_from_audio_specific_config(self) -> None:
        self.assertEqual(
            qualification.parse_aac_channel_configuration("\n00000000: 1190 56e5 00  ..V..\n"),
            2,
        )
        self.assertEqual(
            qualification.parse_aac_channel_configuration("\n00000000: 1180 04c8 4000  ....@.\n"),
            0,
        )
        self.assertEqual(
            qualification.parse_aac_channel_configuration("\n00000000: 11b8 56e5 00  ..V..\n"),
            7,
        )
        self.assertIsNone(qualification.parse_aac_channel_configuration(None))
        self.assertIsNone(qualification.parse_aac_channel_configuration("not a hexdump"))

    def test_identity_classifier_accepts_expected_remap(self) -> None:
        source_channels = ("SL", "SR")
        output_channels = ("SL", "SR")
        frame_count = int(
            (2 * qualification.IDENTITY_GUARD_SECONDS + len(source_channels) * qualification.IDENTITY_SLOT_SECONDS)
            * qualification.SAMPLE_RATE
        )
        samples = [0.0] * (frame_count * len(output_channels))
        for source_index, output_index in enumerate((0, 1)):
            start_seconds = qualification.IDENTITY_GUARD_SECONDS + source_index * qualification.IDENTITY_SLOT_SECONDS
            start_frame = int(start_seconds * qualification.SAMPLE_RATE)
            end_frame = int((start_seconds + qualification.IDENTITY_BURST_SECONDS) * qualification.SAMPLE_RATE)
            for frame_index in range(start_frame, end_frame):
                samples[frame_index * len(output_channels) + output_index] = 0.5

        result = qualification.analyze_identity_samples(
            samples,
            output_channels,
            source_channels,
            identity_map(SL="SL", SR="SR"),
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observed_map"], {"SL": ["SL"], "SR": ["SR"]})

    def test_identity_classifier_rejects_a_missing_expected_output(self) -> None:
        source_channels = ("FC",)
        output_channels = ("FL", "FR")
        frame_count = int(
            (2 * qualification.IDENTITY_GUARD_SECONDS + qualification.IDENTITY_SLOT_SECONDS) * qualification.SAMPLE_RATE
        )
        samples = [0.0] * (frame_count * len(output_channels))
        start_frame = int(qualification.IDENTITY_GUARD_SECONDS * qualification.SAMPLE_RATE)
        end_frame = int(
            (qualification.IDENTITY_GUARD_SECONDS + qualification.IDENTITY_BURST_SECONDS) * qualification.SAMPLE_RATE
        )
        for frame_index in range(start_frame, end_frame):
            samples[frame_index * len(output_channels)] = 0.5

        result = qualification.analyze_identity_samples(
            samples,
            output_channels,
            source_channels,
            identity_map(FC=("FL", "FR")),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["mismatches"]["FC"]["missing_outputs"], ["FR"])


if __name__ == "__main__":
    unittest.main()

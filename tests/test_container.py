import unittest
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import container


class MuxCommandTests(unittest.TestCase):
    def test_final_mux_forces_video_sync_samples_for_quicktime_seeking(self) -> None:
        with (
            patch.object(container.config, "MP4BOX_PATH", Path("/tools/MP4Box")),
            patch.object(
                container,
                "get_audio_stream_data",
                return_value=[{"index": 0, "tags": {"language": "eng"}, "channel_layout": "7.1"}],
            ),
            patch.object(container, "sorted_files_by_creation_filtered_on_suffix", return_value=[]),
            patch.object(container, "run_command") as run_command,
        ):
            container.mux_video_audio_subs(
                Path("movie_MV-HEVC.mov"),
                Path("audio_PCM.mov"),
                Path("movie_AVP.mov"),
                Path("."),
            )

        command = run_command.call_args.args[0]
        self.assertEqual(command[:4], [Path("/tools/MP4Box"), "-new", "-add", "movie_MV-HEVC.mov:forcesync"])
        self.assertIn("audio_PCM.mov#1:lang=eng:group=1:alternate_group=1", command)
        self.assertEqual(command[-1], Path("movie_AVP.mov"))


if __name__ == "__main__":
    unittest.main()

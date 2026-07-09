import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.modules import process
from bd_to_avp.modules.disc import DiscInfo


class ProcessAudioWiringTests(unittest.TestCase):
    def test_direct_audio_source_is_replaced_by_aac_and_removed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.mkv"
            output_folder = temp_path / "Movie"
            video_path = output_folder / "Movie_mvc.h264"
            left_path = output_folder / "Movie_left.mov"
            right_path = output_folder / "Movie_right.mov"
            mv_hevc_path = output_folder / "Movie_MV-HEVC.mov"
            aac_path = output_folder / "Movie_audio_AAC.mov"
            final_path = output_folder / "Movie_AVP.mov"
            source_path.write_bytes(b"source")
            output_folder.mkdir()

            with ExitStack() as stack:
                stack.enter_context(patch.object(process.config, "source_path", source_path))
                stack.enter_context(patch.object(process.config, "output_root_path", temp_path))
                stack.enter_context(patch.object(process.config, "overwrite", True))
                stack.enter_context(patch.object(process.config, "keep_files", False))
                stack.enter_context(patch.object(process.config, "remove_original", True))
                stack.enter_context(patch.object(process.config, "language_code", "eng"))
                stack.enter_context(patch.object(process.preflight, "verify_runtime_ready"))
                stack.enter_context(
                    patch.object(process, "get_disc_and_mvc_video_info", return_value=DiscInfo(name="Movie"))
                )
                stack.enter_context(
                    patch.object(process, "prepare_output_folder_for_source", return_value=output_folder)
                )
                stack.enter_context(patch.object(process, "file_exists_normalized", return_value=False))
                stack.enter_context(patch.object(process, "create_mkv_file", return_value=source_path))
                stack.enter_context(patch.object(process, "get_video_color_depth", return_value=8))
                stack.enter_context(patch.object(process, "detect_crop_parameters", return_value=None))
                stack.enter_context(
                    patch.object(process, "create_mvc_and_audio", return_value=(source_path, video_path))
                )
                stack.enter_context(patch.object(process, "create_srt_from_mkv"))
                stack.enter_context(
                    patch.object(process, "create_left_right_files", return_value=(left_path, right_path))
                )
                stack.enter_context(patch.object(process, "create_mv_hevc_file", return_value=mv_hevc_path))
                stack.enter_context(patch.object(process, "create_upscaled_file", return_value=mv_hevc_path))
                transcode = stack.enter_context(
                    patch.object(process, "create_transcoded_audio_file", return_value=aac_path)
                )
                mux = stack.enter_context(patch.object(process, "create_muxed_file", return_value=final_path))
                stack.enter_context(patch.object(process, "move_file_to_output_root_folder"))
                stack.enter_context(patch.dict(process.os.environ, {}, clear=False))
                process.process_each()

                transcode.assert_called_once_with(source_path, output_folder)
                mux.assert_called_once_with(aac_path, mv_hevc_path, output_folder, "Movie")
                self.assertFalse(source_path.exists())


if __name__ == "__main__":
    unittest.main()

from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.command import run_ffmpeg_print_errors


def transcode_audio(input_path: Path, transcoded_audio_path: Path, bitrate: int):
    audio_input = ffmpeg.input(str(input_path))
    audio_transcoded = ffmpeg.output(
        audio_input["a"],
        str(f"file:{transcoded_audio_path}"),
        acodec="aac",
        audio_bitrate=f"{bitrate}k",
    )
    run_ffmpeg_print_errors(audio_transcoded, f"transcode audio to {bitrate}kbps", overwrite_output=True)


def create_transcoded_audio_file(original_audio_path: Path, output_folder: Path) -> Path:
    trancoded_audio_path = output_folder / f"{output_folder.stem}_audio_AAC.mov"
    if config.transcode_audio and config.start_stage.value <= Stage.TRANSCODE_AUDIO.value:
        transcode_audio(original_audio_path, trancoded_audio_path, config.audio_bitrate)
        if not config.keep_files:
            original_audio_path.unlink(missing_ok=True)
        return trancoded_audio_path
    else:
        if config.transcode_audio and trancoded_audio_path.exists():
            return trancoded_audio_path
        return original_audio_path

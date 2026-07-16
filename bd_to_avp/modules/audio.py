from pathlib import Path

import ffmpeg

from bd_to_avp.modules.config import Stage, config, is_direct_audio_transcode_enabled
from bd_to_avp.modules.command import run_ffmpeg_print_errors


def transcode_audio(input_path: Path, transcoded_audio_path: Path, bitrate: int, audio_selector: str = "a") -> None:
    audio_input = ffmpeg.input(str(input_path))
    audio_transcoded = ffmpeg.output(
        audio_input[audio_selector],
        str(f"file:{transcoded_audio_path}"),
        acodec="aac",
        audio_bitrate=f"{bitrate}k",
    )
    run_ffmpeg_print_errors(audio_transcoded, f"transcode audio to {bitrate}kbps", overwrite_output=True)


def create_transcoded_audio_file(original_audio_path: Path, output_folder: Path) -> Path:
    transcoded_audio_path = output_folder / f"{output_folder.stem}_audio_AAC.m4a"
    legacy_transcoded_audio_path = output_folder / f"{output_folder.stem}_audio_AAC.mov"
    direct_audio_transcode = is_direct_audio_transcode_enabled()

    if config.transcode_audio and config.start_stage.value <= Stage.TRANSCODE_AUDIO.value:
        temporary_audio_path = transcoded_audio_path.with_suffix(".part.m4a")
        try:
            transcode_audio(original_audio_path, temporary_audio_path, config.audio_bitrate)
            temporary_audio_path.replace(transcoded_audio_path)
        finally:
            temporary_audio_path.unlink(missing_ok=True)

        if not config.keep_files and not direct_audio_transcode:
            original_audio_path.unlink(missing_ok=True)
        return transcoded_audio_path

    if config.transcode_audio and transcoded_audio_path.exists():
        return transcoded_audio_path
    if config.transcode_audio and legacy_transcoded_audio_path.exists():
        raise FileNotFoundError(
            "Legacy AAC audio artifact found. Restart from Transcode Audio to regenerate a compatible M4A file: "
            f"{legacy_transcoded_audio_path}"
        )
    if direct_audio_transcode and config.transcode_audio:
        raise FileNotFoundError(f"Direct AAC audio artifact not found: {transcoded_audio_path}")
    return original_audio_path

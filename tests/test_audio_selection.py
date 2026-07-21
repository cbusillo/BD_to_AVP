import unittest
import tempfile
import json

from pathlib import Path
from unittest.mock import Mock, patch

from bd_to_avp.modules.audio_selection import (
    AudioFallbackReason,
    AudioSelectionError,
    audio_selection_manifest_path,
    emit_audio_selection_warning,
    load_audio_selection_manifest,
    persist_audio_selection,
    select_audio_streams,
)


class AudioSelectionTests(unittest.TestCase):
    def test_multilingual_fixture_preserves_all_mode_and_reduces_preferred_payload(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "multilingual_audio_selection_v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        streams = fixture["streams"]

        all_languages = select_audio_streams(streams, None)
        preferred_english = select_audio_streams(streams, "eng")

        all_expectation = fixture["expectations"]["all_languages"]
        preferred_expectation = fixture["expectations"]["preferred_english"]
        self.assertEqual([stream.stream_index for stream in all_languages.streams], all_expectation["stream_indexes"])
        self.assertEqual(
            sum(stream.stream["estimated_payload_bytes"] for stream in all_languages.streams),
            all_expectation["estimated_payload_bytes"],
        )
        self.assertEqual(
            [stream.stream_index for stream in preferred_english.streams],
            preferred_expectation["stream_indexes"],
        )
        preferred_payload = sum(stream.stream["estimated_payload_bytes"] for stream in preferred_english.streams)
        self.assertEqual(preferred_payload, preferred_expectation["estimated_payload_bytes"])
        self.assertLess(preferred_payload, all_expectation["estimated_payload_bytes"])

    def test_all_languages_preserves_every_stream_in_source_order(self) -> None:
        streams = [
            audio_stream(7, "jpn", title="Japanese 5.1"),
            audio_stream(2, "eng", title="English Commentary"),
            audio_stream(11, "eng", title="English Stereo"),
        ]

        selection = select_audio_streams(streams, None)

        self.assertEqual([stream.audio_position for stream in selection.streams], [0, 1, 2])
        self.assertEqual([stream.stream_index for stream in selection.streams], [7, 2, 11])
        self.assertEqual([stream.stream for stream in selection.streams], streams)
        self.assertIsNone(selection.preferred_language)
        self.assertFalse(selection.used_fallback)
        self.assertEqual(selection.source_stream_count, 3)

    def test_preferred_language_keeps_all_canonical_matches_without_title_inference(self) -> None:
        streams = [
            audio_stream(1, "jpn", title="English Director Commentary"),
            audio_stream(4, "en-US", title="Main English"),
            audio_stream(9, "eng", title="English Stereo"),
            audio_stream(12, "fra", title="French"),
        ]

        selection = select_audio_streams(streams, "en")

        self.assertEqual(selection.preferred_language, "eng")
        self.assertEqual([stream.audio_position for stream in selection.streams], [1, 2])
        self.assertEqual([stream.stream_index for stream in selection.streams], [4, 9])
        self.assertEqual([stream.language for stream in selection.streams], ["eng", "eng"])
        self.assertFalse(selection.used_fallback)

    def test_missing_language_falls_back_to_source_default_even_when_not_first(self) -> None:
        streams = [
            audio_stream(2, "fra"),
            audio_stream(5, "eng", default=True),
            audio_stream(8, "deu"),
        ]

        selection = select_audio_streams(streams, "jpn")

        self.assertEqual([stream.audio_position for stream in selection.streams], [1])
        self.assertEqual([stream.stream_index for stream in selection.streams], [5])
        self.assertEqual(selection.fallback_reason, AudioFallbackReason.SOURCE_DEFAULT)

    def test_missing_language_falls_back_to_first_when_no_default_exists(self) -> None:
        streams = [
            audio_stream(3, "xyz", title="English Dub"),
            audio_stream(6, None),
        ]

        selection = select_audio_streams(streams, "eng")

        self.assertEqual([stream.audio_position for stream in selection.streams], [0])
        self.assertEqual(selection.streams[0].language, "und")
        self.assertEqual(selection.fallback_reason, AudioFallbackReason.FIRST_STREAM)

    def test_empty_source_is_rejected_instead_of_producing_silent_output(self) -> None:
        with self.assertRaisesRegex(AudioSelectionError, "does not contain an audio stream"):
            select_audio_streams([], "eng")

    def test_fallback_warning_is_visible_and_structured(self) -> None:
        selection = select_audio_streams(
            [audio_stream(1, "fra"), audio_stream(3, "eng", default=True)],
            "jpn",
        )
        activity = Mock()

        emit_audio_selection_warning(selection, activity, stage="transcode_audio")

        activity.warning.assert_called_once_with(
            "No audio tracks matched the preferred language Japanese (jpn). Keeping the source-default audio track "
            "(English (eng)) instead.",
            stage="transcode_audio",
            code="audio_language_fallback",
            preferred_language="jpn",
            selected_language="eng",
            selected_stream_index=3,
            selected_audio_position=1,
            fallback_reason="source_default",
            action="keep_source_default_audio",
        )

    def test_cli_fallback_warning_is_not_silent(self) -> None:
        selection = select_audio_streams([audio_stream(0, "eng")], "jpn")

        with patch("bd_to_avp.modules.audio_selection.cli_message") as message:
            emit_audio_selection_warning(selection, None, stage="create_final_file")

        message.assert_called_once()
        self.assertIn("No audio tracks matched", message.call_args.args[0])

    def test_preferred_selection_manifest_round_trips_and_all_mode_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio_path = Path(temporary_directory) / "Movie_audio_AAC.m4a"
            selection = select_audio_streams(
                [audio_stream(1, "eng"), audio_stream(4, "jpn")],
                "eng",
            )

            persist_audio_selection(audio_path, selection)

            manifest = load_audio_selection_manifest(audio_path)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.preferred_language, "eng")
            self.assertEqual(manifest.source_stream_count, 2)
            self.assertEqual(manifest.selected_stream_count, 1)
            self.assertTrue(manifest.omitted_streams)

            persist_audio_selection(audio_path, None)

            self.assertFalse(audio_selection_manifest_path(audio_path).exists())
            self.assertIsNone(load_audio_selection_manifest(audio_path))

    def test_manifest_ignores_unknown_additive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio_path = Path(temporary_directory) / "Movie_audio_AAC.m4a"
            manifest_path = audio_selection_manifest_path(audio_path)
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "preferred_language": "eng",
                        "source_stream_count": 2,
                        "selected_stream_count": 1,
                        "created_by": "future-version",
                    }
                ),
                encoding="utf-8",
            )

            manifest = load_audio_selection_manifest(audio_path)

            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.preferred_language, "eng")


def audio_stream(
    index: int,
    language: str | None,
    *,
    default: bool = False,
    title: str | None = None,
) -> dict[str, object]:
    tags: dict[str, object] = {}
    if language is not None:
        tags["language"] = language
    if title is not None:
        tags["title"] = title
    return {
        "index": index,
        "codec_type": "audio",
        "tags": tags,
        "disposition": {"default": 1 if default else 0},
    }


if __name__ == "__main__":
    unittest.main()

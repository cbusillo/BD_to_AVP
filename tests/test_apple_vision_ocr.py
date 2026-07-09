import types
import unittest
from unittest.mock import patch

import numpy as np
from babelfish import Language

from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr, OcrWord


class MockVisionCandidate:
    def string(self) -> str:
        return "OK"

    def confidence(self) -> float:
        return 1.0


class MockVisionObservation:
    def topCandidates_(self, _count: int) -> list[MockVisionCandidate]:
        return [MockVisionCandidate()]

    def boundingBox(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            origin=types.SimpleNamespace(x=0.0, y=0.0),
            size=types.SimpleNamespace(width=1.0, height=1.0),
        )


class MockVisionRequestInstance:
    language_codes: list[str] | None = None

    def setRecognitionLevel_(self, _level: int) -> None:
        pass

    def setUsesLanguageCorrection_(self, _enabled: bool) -> None:
        pass

    def setRecognitionLanguages_(self, language_codes: list[str]) -> None:
        self.language_codes = language_codes

    def supportedRecognitionLanguagesAndReturnError_(self, _error: object) -> tuple[list[str], None]:
        return ["en"], None

    def results(self) -> list[MockVisionObservation]:
        return [MockVisionObservation()]


class MockVisionRequest:
    @classmethod
    def alloc(cls) -> type["MockVisionRequest"]:
        return cls

    @classmethod
    def init(cls) -> MockVisionRequestInstance:
        return MockVisionRequestInstance()


class MockVisionHandlerInstance:
    def performRequests_error_(self, _requests: list[MockVisionRequestInstance], _error: object) -> tuple[bool, None]:
        return True, None


class MockVisionHandler:
    last_options: object = object()

    @classmethod
    def alloc(cls) -> type["MockVisionHandler"]:
        return cls

    @classmethod
    def initWithCGImage_options_(cls, _image: object, options: object) -> MockVisionHandlerInstance:
        cls.last_options = options
        return MockVisionHandlerInstance()


class MockQuartz:
    kCGBitmapByteOrderDefault = 0
    kCGImageAlphaNone = 0
    kCGRenderingIntentDefault = 0

    @staticmethod
    def CGDataProviderCreateWithData(_info: object, _data: bytes, _size: int, _release_callback: object) -> object:
        return object()

    @staticmethod
    def CGColorSpaceCreateDeviceGray() -> object:
        return object()

    @staticmethod
    def CGImageCreate(*_args: object) -> object:
        return object()


class AppleVisionOcrTests(unittest.TestCase):
    def test_pixel_box_flips_vision_coordinates_to_image_top_left(self) -> None:
        normalized_box = types.SimpleNamespace(
            origin=types.SimpleNamespace(x=0.25, y=0.2),
            size=types.SimpleNamespace(width=0.5, height=0.3),
        )

        self.assertEqual(AppleVisionOcr._pixel_box(normalized_box, (100, 200)), (50, 50, 100, 30))

    def test_tsv_data_from_words_matches_existing_tsv_contract(self) -> None:
        data = AppleVisionOcr._tsv_data_from_words(
            [
                OcrWord(
                    text="Hello",
                    confidence=91,
                    left=10,
                    top=20,
                    width=30,
                    height=12,
                    line_num=1,
                    word_num=1,
                )
            ]
        )

        self.assertEqual(data["level"], [5])
        self.assertEqual(data["page_num"], [1])
        self.assertEqual(data["line_num"], [1])
        self.assertEqual(data["word_num"], [1])
        self.assertEqual(data["left"], [10])
        self.assertEqual(data["top"], [20])
        self.assertEqual(data["conf"], [91])
        self.assertEqual(data["text"], ["Hello"])

    def test_image_request_handler_options_are_none_for_pyobjc_bridge(self) -> None:
        vision = types.SimpleNamespace(
            VNRecognizeTextRequest=MockVisionRequest,
            VNImageRequestHandler=MockVisionHandler,
            VNRequestTextRecognitionLevelAccurate=0,
        )
        quartz = MockQuartz()
        image = np.full((2, 2), 255, dtype=np.uint8)

        with patch.object(AppleVisionOcr, "_load_frameworks", return_value=(vision, quartz)):
            AppleVisionOcr().image_to_data(image, language=None)

        self.assertIs(MockVisionHandler.last_options, None)

    def test_supported_language_hint_is_used(self) -> None:
        request = MockVisionRequestInstance()

        language_code = AppleVisionOcr._recognition_language(Language("eng"), request)

        self.assertEqual(language_code, "en")

    def test_unsupported_language_hint_is_skipped(self) -> None:
        request = MockVisionRequestInstance()

        language_code = AppleVisionOcr._recognition_language(Language("fra"), request)

        self.assertIsNone(language_code)

    def test_line_box_is_split_across_tokens(self) -> None:
        boxes = AppleVisionOcr._split_line_box(["Hello", "world"], 10, 20, 110, 15)

        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0][0], 10)
        self.assertEqual(boxes[0][1], 20)
        self.assertGreater(boxes[1][0], boxes[0][0] + boxes[0][2])
        self.assertEqual(boxes[1][1], 20)


if __name__ == "__main__":
    unittest.main()

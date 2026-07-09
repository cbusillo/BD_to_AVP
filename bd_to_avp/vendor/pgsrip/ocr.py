from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from babelfish import Language


class OcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: int
    left: int
    top: int
    width: int
    height: int
    line_num: int
    word_num: int


class AppleVisionOcr:
    def image_to_data(self, image: np.ndarray, language: Language | None = None) -> dict[str, list[Any]]:
        vision, quartz = self._load_frameworks()
        cg_image = self._cg_image(image, quartz)
        request = vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)

        language_code = self._recognition_language(language, request)
        if language_code:
            request.setRecognitionLanguages_([language_code])

        handler = vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
        success, error = handler.performRequests_error_([request], None)
        if not success:
            raise OcrError(f"Apple Vision OCR failed: {error}")

        words = self._words_from_results(request.results() or [], image.shape)
        return self._tsv_data_from_words(words)

    @staticmethod
    def _load_frameworks() -> tuple[Any, Any]:
        try:
            import Quartz  # type: ignore[import-not-found]
            import Vision  # type: ignore[import-not-found]
        except ImportError as error:
            raise OcrError("Apple Vision OCR requires PyObjC Vision and Quartz frameworks.") from error
        return Vision, Quartz

    @staticmethod
    def _cg_image(image: np.ndarray, quartz: Any) -> Any:
        contiguous_image = np.ascontiguousarray(image.astype(np.uint8, copy=False))
        height, width = contiguous_image.shape[:2]
        bytes_per_row = contiguous_image.strides[0]
        image_bytes = contiguous_image.tobytes()
        provider = quartz.CGDataProviderCreateWithData(None, image_bytes, len(image_bytes), None)
        color_space = quartz.CGColorSpaceCreateDeviceGray()
        return quartz.CGImageCreate(
            width,
            height,
            8,
            8,
            bytes_per_row,
            color_space,
            quartz.kCGBitmapByteOrderDefault | quartz.kCGImageAlphaNone,
            provider,
            None,
            False,
            quartz.kCGRenderingIntentDefault,
        )

    @staticmethod
    def _recognition_language(language: Language | None, request: Any) -> str | None:
        if not language:
            return None
        try:
            language_code = language.ietf
        except (AttributeError, ValueError):
            language_code = None
        try:
            language_code = language_code or language.alpha2
        except (AttributeError, ValueError):
            return None
        try:
            supported_languages, error = request.supportedRecognitionLanguagesAndReturnError_(None)
        except AttributeError:
            return language_code
        if error or not supported_languages:
            return None
        return language_code if language_code in set(supported_languages) else None

    @classmethod
    def _words_from_results(cls, observations: list[Any], image_shape: tuple[int, ...]) -> list[OcrWord]:
        words: list[OcrWord] = []
        line_num = 0
        for observation in observations:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue

            text = str(candidates[0].string()).strip()
            if not text:
                continue

            line_num += 1
            confidence = cls._confidence(candidates[0])
            tokens = text.split()
            if not tokens:
                continue

            left, top, width, height = cls._pixel_box(observation.boundingBox(), image_shape)
            token_boxes = cls._split_line_box(tokens, left, top, width, height)
            for word_num, (token, token_box) in enumerate(zip(tokens, token_boxes, strict=True), start=1):
                words.append(
                    OcrWord(
                        text=token,
                        confidence=confidence,
                        left=token_box[0],
                        top=token_box[1],
                        width=token_box[2],
                        height=token_box[3],
                        line_num=line_num,
                        word_num=word_num,
                    )
                )
        return words

    @staticmethod
    def _confidence(candidate: Any) -> int:
        confidence = candidate.confidence()
        try:
            return max(0, min(100, round(float(confidence) * 100)))
        except (TypeError, ValueError):
            return 100

    @staticmethod
    def _pixel_box(normalized_box: Any, image_shape: tuple[int, ...]) -> tuple[int, int, int, int]:
        image_height, image_width = image_shape[:2]
        left = max(0, math.floor(float(normalized_box.origin.x) * image_width))
        top = max(0, math.floor((1.0 - float(normalized_box.origin.y) - float(normalized_box.size.height)) * image_height))
        width = max(1, math.ceil(float(normalized_box.size.width) * image_width))
        height = max(1, math.ceil(float(normalized_box.size.height) * image_height))
        if left + width > image_width:
            width = max(1, image_width - left)
        if top + height > image_height:
            height = max(1, image_height - top)
        return left, top, width, height

    @staticmethod
    def _split_line_box(tokens: list[str], left: int, top: int, width: int, height: int) -> list[tuple[int, int, int, int]]:
        total_weight = sum(len(token) for token in tokens) + max(0, len(tokens) - 1)
        if total_weight <= 0:
            return [(left, top, width, height)]

        boxes: list[tuple[int, int, int, int]] = []
        cursor = left
        consumed = 0
        for index, token in enumerate(tokens):
            weight = len(token)
            if index < len(tokens) - 1:
                token_width = max(1, round(width * weight / total_weight))
                space_width = max(1, round(width / total_weight))
            else:
                token_width = max(1, left + width - cursor)
                space_width = 0
            if consumed + token_width > width:
                token_width = max(1, width - consumed)
            boxes.append((cursor, top, token_width, height))
            cursor += token_width + space_width
            consumed = cursor - left
        return boxes

    @staticmethod
    def _tsv_data_from_words(words: list[OcrWord]) -> dict[str, list[Any]]:
        data: dict[str, list[Any]] = {
            "level": [],
            "page_num": [],
            "block_num": [],
            "par_num": [],
            "line_num": [],
            "word_num": [],
            "left": [],
            "top": [],
            "width": [],
            "height": [],
            "conf": [],
            "text": [],
        }
        for word in words:
            data["level"].append(5)
            data["page_num"].append(1)
            data["block_num"].append(word.line_num)
            data["par_num"].append(1)
            data["line_num"].append(word.line_num)
            data["word_num"].append(word.word_num)
            data["left"].append(word.left)
            data["top"].append(word.top)
            data["width"].append(word.width)
            data["height"].append(word.height)
            data["conf"].append(word.confidence)
            data["text"].append(word.text)
        return data

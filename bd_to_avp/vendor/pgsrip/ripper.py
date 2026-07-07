from __future__ import annotations

import json
import logging
import os
import typing

import cv2

import numpy as np

from pysrt import SubRipFile, SubRipItem

import pytesseract as tess

from bd_to_avp.vendor.pgsrip.media import Pgs, PgsSubtitleItem
from bd_to_avp.vendor.pgsrip.options import Options, TesseractEngineMode, TesseractPageSegmentationMode
from bd_to_avp.vendor.pgsrip.tsv import TsvData


logger = logging.getLogger(__name__)


class ImageArea:

    def __init__(self, items: typing.List[PgsSubtitleItem], gap: typing.Tuple[int, int]):
        self.gap = gap
        self.width = sum([(item.shape[3] - item.shape[1]) for item in items]) + (len(items) - 1) * gap[1]
        self.shape = (
            min([item.shape[0] for item in items]), items[0].shape[1],
            max([item.shape[2] for item in items]), min([item.shape[1] for item in items]) + self.width)
        self.items = items

    def __str__(self):
        return str(self.shape)

    def __repr__(self):
        return f'<{self.__class__.__name__} [{self}]>'

    @property
    def height(self):
        return self.shape[2] - self.shape[0]

    def create_area_image(self, start: typing.Tuple[int, int]):
        area_image = np.full((self.height, self.width), 255, dtype=np.uint8)

        current_width = 0
        for item in self.items:
            h_start, w_start, h_end, w_end = self.get_shape(item, current_width=current_width)
            item.place = (
                start[0] + h_start, start[1] + w_start,
                start[0] + h_end, start[1] + w_end
            )
            area_image[h_start:h_end, w_start:w_end] = item.image.data
            current_width += item.width + self.gap[1]

        return area_image

    def get_shape(self, item: PgsSubtitleItem, current_width=0, full_shape=False):
        start_y = 0
        start_x = current_width
        h_start = start_y + ((item.shape[0] - self.shape[0]) if not full_shape else 0)
        w_start = start_x
        h_end = h_start + (item.image.shape[0] if not full_shape else self.height)
        w_end = w_start + (item.image.shape[1] if not full_shape else self.width)

        return h_start, w_start, h_end, w_end


class FullImage:

    def __init__(self, areas: typing.List[ImageArea], gap: typing.Tuple[int, int]):
        border = 100
        total_height = sum([area.height for area in areas]) + (len(areas) - 1) * gap[0] + 2 * border
        total_width = max([area.width for area in areas]) + 2 * border
        full_image = np.full((total_height, total_width), 255, dtype=np.uint8)
        h_start = border
        w_start = border
        for area in areas:
            h_end = h_start + area.height
            w_end = w_start + area.width
            full_image[h_start:h_end, w_start:w_end] = area.create_area_image((h_start, w_start))
            h_start = h_end + gap[0]

        self.data = full_image

    @classmethod
    def from_items(cls, items: typing.List[PgsSubtitleItem], gap: typing.Tuple[int, int], max_width: int):
        areas: typing.List[ImageArea] = []
        remaining = list(items)
        remaining.sort(key=lambda x: x.height)
        while len(remaining) > 0:
            first_item = remaining.pop(0)
            area_items = [first_item] + [item for item in remaining if item.intersect(first_item)]
            remaining = [item for item in remaining if not item.intersect(first_item)]
            current_items: typing.List[PgsSubtitleItem] = []
            current_width = 0
            for area_item in area_items:
                current_width += area_item.width + gap[1]
                if current_width > max_width:
                    areas.append(ImageArea(current_items, gap))
                    current_width = area_item.width
                    current_items = [area_item]
                else:
                    current_items.append(area_item)

            if len(current_items) > 0:
                areas.append(ImageArea(current_items, gap))

        return FullImage(areas, gap)

    def __repr__(self):
        return f'<{self.__class__.__name__} [{self}]>'

    def __str__(self):
        return f'{self.data.shape}]'


class PgsToSrtRipper:

    def __init__(self, pgs: Pgs, options: Options):
        self.pgs = pgs
        self.confidence = min(max(options.confidence or 65, 0), 100)
        self.max_tess_width = min(max(options.tesseract_width or 31 * 1024, 10 * 1024), 31 * 1024)
        self.omp_thread_limit = options.max_workers
        self.oem = options.tesseract_oem or TesseractEngineMode.NEURAL
        self.psm = options.tesseract_psm or TesseractPageSegmentationMode.SINGLE_UNIFORM_BLOCK_OF_TEXT
        max_height = max([item.height for item in self.pgs.items], default=0) // 2
        self.gap = (max_height // 2 + 30, max_height // 2 + 100)
        self.keep_temp_files = options.keep_temp_files

    def process(self,
                subs: SubRipFile,
                items: typing.List[PgsSubtitleItem],
                post_process,
                confidence: int,
                max_width: int,
                oem: TesseractEngineMode,
                psm: TesseractPageSegmentationMode):
        full_image = FullImage.from_items(items, self.gap, max_width)

        config = {
            'output_type': tess.Output.DICT,
            'config': f'--psm {psm.value} --oem {oem.value}'
        }

        if self.pgs.language:
            config.update({'lang': self.pgs.language.alpha3})

        if self.omp_thread_limit:
            os.environ['OMP_THREAD_LIMIT'] = str(self.omp_thread_limit)
        if self.keep_temp_files:
            png_file = os.path.join(self.pgs.temp_folder,
                                    f'{os.path.basename(subs.path)}-{len(items)}'
                                    f'-psm{psm.value}-{oem.name}-{confidence}.png')
            logger.debug('Writing temporary png file %s', png_file)
            cv2.imwrite(png_file, full_image.data)

        data = TsvData(tess.image_to_data(full_image.data, **config), confidence=confidence)

        if self.keep_temp_files:
            results_file = os.path.join(self.pgs.temp_folder,
                                        f'{os.path.basename(subs.path)}-{len(items)}-{confidence}.json')
            logger.debug('Writing temporary results file %s', results_file)
            with open(results_file, mode='w', encoding='utf8') as f:
                json.dump([i.__dict__ for i in data.items], f, indent=2, ensure_ascii=False)

        remaining = []
        for item in items:
            text = self.accept(data, item, confidence)
            if text is None:
                remaining.append(item)
                continue

            text = item.text
            if post_process:
                text = post_process(text)
            if text:
                item = SubRipItem(0, item.start, item.end, text)
                subs.append(item)

        return remaining

    @classmethod
    def accept(cls, data: TsvData, item: PgsSubtitleItem, confidence: int):
        rows = data.select(item.place) if item.place else []
        lines = []
        words = []
        last_row = None
        for row in rows:
            if row.conf < confidence:
                if not data.has_word(row.text):
                    return None

            if last_row is not None and (last_row.page_num < row.page_num
                                         or last_row.block_num < row.block_num
                                         or last_row.par_num < row.par_num
                                         or last_row.line_num < row.line_num) \
                    and len(words) > 0:
                lines.append(' '.join(words))
                words.clear()
            words.append(row.text)
            last_row = row

        if len(words) > 0:
            lines.append(' '.join(words))
            words.clear()

        item.text = '\n'.join(lines).strip()
        return item.text

    def rip(self, post_process: typing.Callable[[str], str]):
        subs = SubRipFile(path=str(self.pgs.media_path.translate(extension='srt')))
        oem, psm, confidence, max_width = self.oem, self.psm, self.confidence, self.max_tess_width
        items = self.pgs.items
        previous_size = len(items)
        while previous_size > 0:
            items = self.process(subs, items, post_process, confidence, max_width, oem, psm)
            if not items:
                break

            current_size = len(items)
            if current_size < 20:
                max_width = min(sum([item.width + self.gap[1] for item in items]), self.max_tess_width)
                confidence = 0
                remaining_items = self.process(subs, items, post_process, confidence, max_width, oem, psm)
                if remaining_items:
                    logger.warning('Subtitles were not ripped: %r', remaining_items)
                break
            elif current_size > previous_size * 0.8:
                max_width = min(sum([item.width + self.gap[1] for item in items]), self.max_tess_width) // 2
                confidence = max(0, confidence - 5)
            previous_size = current_size

        subs.clean_indexes()

        return subs

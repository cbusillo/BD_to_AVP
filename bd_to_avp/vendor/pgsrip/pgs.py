import enum
import logging
import typing

import cv2

import numpy as np
from numpy import ndarray

from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.utils import from_hex, safe_get, to_time

logger = logging.getLogger(__name__)


@enum.unique
class SegmentType(enum.Enum):
    PDS = int('0x14', 16)
    ODS = int('0x15', 16)
    PCS = int('0x16', 16)
    WDS = int('0x17', 16)
    END = int('0x80', 16)


@enum.unique
class CompositionState(enum.Enum):
    NORMAL_CASE = from_hex(b'\x00')
    ACQUISITION_POINT = from_hex(b'\x40')
    EPOCH_START = from_hex(b'\x80')


@enum.unique
class ObjectSequenceType(enum.Enum):
    LAST = from_hex(b'\x40')
    FIRST = from_hex(b'\x80')
    FIRST_AND_LAST = from_hex(b'\xc0')


class Palette(typing.NamedTuple):
    y: int
    cr: int
    cb: int
    alpha: int


class PgsReader:

    @classmethod
    def read_segments(cls, data: bytes, media_path: MediaPath):
        count = 0
        b = data
        while b:
            if b[:2] != b'PG':
                logger.warning('%s Ignoring invalid PGS segment data: %s', media_path, b)
                break

            if len(b) < 13:
                logger.warning('%s Ignoring invalid PGS segment data with less than 13 bytes: %s', media_path, b)
                break

            segment_type = SEGMENT_TYPE[SegmentType(b[10])]
            size = 13 + from_hex(b[11:13])
            yield segment_type(b[:size])
            count += size
            b = b[size:]

    @classmethod
    def decode(cls, data: bytes, media_path: MediaPath):
        segments: typing.List[BaseSegment] = []
        index = 0
        for s in cls.read_segments(data, media_path):
            segments.append(s)
            if s.type == SegmentType.END:
                yield DisplaySet(index, segments)
                segments = []
                index += 1


class PgsImage:

    def __init__(self, data: bytes, palettes: typing.List[Palette]):
        self.rle_data = data
        self.palettes = palettes
        self._data: typing.Optional[ndarray] = None

    @property
    def data(self):
        if self._data is None:
            self._data = self.decode_rle_image(self.rle_data, self.palettes)
        return self._data

    @classmethod
    def decode_rle_image(cls, data: bytes, palettes: typing.List[Palette], binary=True):
        image_array: typing.List[int] = []
        alpha_array: typing.List[int] = []
        dimension = 1 if binary else 3
        cols = 1
        i = 0
        while i < len(data):
            length, color, count = cls.decode_rle_position(data, i)
            if not length and cols < 2:
                cols = len(image_array) // dimension
            palette = palettes[color]
            image_color = cls.get_color(palette, binary)
            image_array.extend(image_color * length)
            if not binary:
                alpha_array.extend([palette[3]] * length)
            i += count

        rows = (len(image_array) // dimension + cols - 1) // cols
        if cols * rows * dimension != len(image_array):
            # corrupted image
            delta = cols * rows * dimension - len(image_array)
            image_array.extend((cls.get_color(palettes[0], binary) * dimension) * delta)

        img = np.array(image_array, dtype=np.uint8).reshape((rows, cols) if binary else (rows, cols, dimension))
        if binary:
            return img

        image = cv2.cvtColor(img, cv2.COLOR_YCR_CB2BGR)
        a_channel = np.array(alpha_array, dtype=np.uint8).reshape(rows, cols)
        b_channel, g_channel, r_channel = cv2.split(image)
        image = cv2.merge((b_channel, g_channel, r_channel, a_channel))
        return image

    @classmethod
    def get_color(cls, palette: Palette, binary: bool):
        return ([0] if palette[0] > 127 else [255]) if binary else palette[:3]

    @classmethod
    def decode_rle_position(cls, data: bytes, i: int):
        first = safe_get(data, i)
        if first:
            return 1, first, 1

        second = safe_get(data, i + 1)
        if second < 64:
            return second, 0, 2

        third = safe_get(data, i + 2)
        if second < 128:
            return ((second - 64) << 8) + third, 0, 3
        elif second < 192:
            return second - 128, third, 3

        fourth = safe_get(data, i + 3)
        return ((second - 192) << 8) + third, fourth, 4

    @property
    def shape(self):
        return self.data.shape


class BaseSegment:

    def __init__(self, b: bytes):
        self.bytes = b

    @property
    def presentation_timestamp(self):
        return to_time(from_hex(self.bytes[2:6]) / 90)

    @property
    def decoding_timestamp(self):
        return to_time(from_hex(self.bytes[6:10]) / 90)

    @property
    def type(self):
        return SegmentType(self.bytes[10])

    @property
    def size(self):
        return from_hex(self.bytes[11:13])

    @property
    def data(self):
        return self.bytes[13:]

    def to_json(self):
        attributes = {
            'type': 'type',
            'pts': 'presentation_timestamp',
            'dts': 'decoding_timestamp',
            'size': 'size',
            **self.attributes()
        }

        def to_value(v: typing.Any):
            return v.name if isinstance(v, enum.Enum) else v

        return {
            k: to_value(getattr(self, v)) for k, v in attributes.items() if getattr(self, v) is not None
        }

    def attributes(self):
        raise NotImplementedError

    def __len__(self):
        return self.size

    def __bool__(self):
        return True

    def __str__(self):
        strings = []
        for k, v in self.to_json().items():
            if v is not None:
                strings.append(f'{k}={v}')

        return ', '.join(strings)

    def __repr__(self):
        return f'<{self.__class__.__name__}: [{self}]>'


class PresentationCompositionSegment(BaseSegment):

    @property
    def width(self):
        return from_hex(self.data[0:2])

    @property
    def height(self):
        return from_hex(self.data[2:4])

    @property
    def frame_rate(self):
        return self.data[4]

    @property
    def composition_number(self):
        return from_hex(self.data[5:7])

    @property
    def composition_state(self):
        return CompositionState(self.data[7])

    @property
    def palette_update(self):
        return bool(self.data[8])

    @property
    def palette_id(self):
        return self.data[9]

    @property
    def number_composition_objects(self):
        return self.data[10]

    def attributes(self):
        return {
            'width': 'width',
            'height': 'height',
            'frame_rate': 'frame_rate',
            'number': 'composition_number',
            'state': 'composition_state',
            'palette_update': 'palette_update',
            'palette_id': 'palette_id',
            'num_objects': 'number_composition_objects'
        }

    def is_start(self):
        return self.composition_state in (CompositionState.EPOCH_START, CompositionState.ACQUISITION_POINT)


class WindowDefinitionSegment(BaseSegment):

    @property
    def num_windows(self):
        return self.data[0]

    @property
    def window_id(self):
        return self.data[1]

    @property
    def x_offset(self):
        return from_hex(self.data[2:4])

    @property
    def y_offset(self):
        return from_hex(self.data[4:6])

    @property
    def width(self):
        return from_hex(self.data[6:8])

    @property
    def height(self):
        return from_hex(self.data[8:10])

    def attributes(self):
        return {
            'num_windows': 'num_windows',
            'window_id': 'window_id',
            'x_offset': 'x_offset',
            'y_offset': 'y_offset',
            'width': 'width',
            'height': 'height'
        }


class PaletteDefinitionSegment(BaseSegment):

    def __init__(self, b: bytes):
        super().__init__(b)
        self.palettes = [Palette(0, 0, 0, 0)] * 256
        # Slice from byte 2 til end of segment. Divide by 5 to determine number of palette entries
        # Iterate entries. Explode the 5 bytes into namedtuple Palette. Must be exploded
        for entry in range(len(self.data[2:]) // 5):
            i = 2 + entry * 5
            self.palettes[self.data[i]] = Palette(*self.data[i + 1:i + 5])

    @property
    def palette_id(self):
        return self.data[0]

    @property
    def version(self):
        return self.data[1]

    def attributes(self):
        return {
            'palette_id': 'palette_id',
            'version': 'version'
        }


class ObjectDefinitionSegment(BaseSegment):

    @property
    def id(self):
        return from_hex(self.data[0:2])

    @property
    def version(self):
        return self.data[2]

    @property
    def sequence_type(self):
        return ObjectSequenceType(self.data[3])

    @property
    def data_len(self):
        if self.sequence_type != ObjectSequenceType.LAST:
            return from_hex(self.data[4:7])

        return None

    @property
    def width(self):
        if self.sequence_type != ObjectSequenceType.LAST:
            return from_hex(self.data[7:9])

        return None

    @property
    def height(self):
        if self.sequence_type != ObjectSequenceType.LAST:
            return from_hex(self.data[9:11])

        return None

    @property
    def img_data(self):
        if self.sequence_type == ObjectSequenceType.LAST:
            return self.data[4:]

        return self.data[11:]

    def attributes(self):
        return {
            'id': 'id',
            'version': 'version',
            'sequence_type': 'sequence_type',
            'data_len': 'data_len',
            'width': 'width',
            'height': 'height'
        }


class EndSegment(BaseSegment):

    def attributes(self):
        return {}


SEGMENT_TYPE = {
    SegmentType.PDS: PaletteDefinitionSegment,
    SegmentType.ODS: ObjectDefinitionSegment,
    SegmentType.PCS: PresentationCompositionSegment,
    SegmentType.WDS: WindowDefinitionSegment,
    SegmentType.END: EndSegment
}


class DisplaySet:

    def __init__(self, index: int, segments: typing.List[BaseSegment]):
        self.index = index
        self.segments = segments

    @property
    def pcs(self):
        return [s for s in self.segments if isinstance(s, PresentationCompositionSegment)][0]

    @property
    def wds(self):
        return [s for s in self.segments if isinstance(s, WindowDefinitionSegment)][0]

    @property
    def pds_segments(self):
        return [s for s in self.segments if isinstance(s, PaletteDefinitionSegment)]

    @property
    def ods_segments(self):
        return [s for s in self.segments if isinstance(s, ObjectDefinitionSegment)]

    @property
    def end(self):
        return [s for s in self.segments if isinstance(s, EndSegment)][0]

    def is_start(self):
        return self.pcs.is_start()

    def is_valid(self):
        valid = True
        counts: typing.Dict[SegmentType, int] = {}
        for s in self.segments:
            counts[s.type] = counts.get(s.type, 0) + 1
            if (isinstance(s, PresentationCompositionSegment)
                    and s.composition_state == CompositionState.ACQUISITION_POINT):
                logger.warning('ACQUISITION_POINT found %s, %r', s, self)

        for t in (SegmentType.PCS, SegmentType.WDS, SegmentType.END):
            count = counts.get(t)
            if not count:
                logger.warning('No %s found for %r', t, self)
                valid = False
            elif count > 1:
                logger.warning('Multiple %s found for %r', t, self)
                valid = False

        return valid

    def to_json(self):
        return {
            'index': self.index,
            'segments': [s.to_json() for s in self.segments]
        }

    def __str__(self):
        strings = [f'DS[{self.index}]']
        for s in self.segments:
            strings.append(f'\t{s}')

        return '\n'.join(strings)

    def __repr__(self):
        return f'<{self.__class__.__name__}: {self}]>'

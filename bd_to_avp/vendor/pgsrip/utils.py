import typing

from pysrt import SubRipTime


def from_hex(b: bytes):
    return int(b.hex(), base=16)


def safe_get(b: bytes, i: int, default_value=0):
    try:
        return b[i]
    except IndexError:
        return default_value


def to_time(value: typing.Optional[int]):
    return SubRipTime.from_ordinal(value) if value else None


T = typing.TypeVar('T')


def pairwise(iterable: typing.Iterable[T]) -> typing.Iterable[typing.Tuple[T, typing.Optional[T]]]:
    """s -> (s0, s1), (s1, s2), (s2, s3), (s2, None)"""
    it = iter(iterable)
    a = next(it, None)
    if a is not None:
        for b in it:
            yield a, b
            a = b

        yield a, None

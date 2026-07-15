from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewRange:
    start_seconds: float
    duration_seconds: float
    source_duration_seconds: float

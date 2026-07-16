from enum import StrEnum


class AudioMode(StrEnum):
    AUTOMATIC = "automatic"
    CONVERT_AAC = "convert_aac"
    PCM = "pcm"

    @property
    def prepares_m4a(self) -> bool:
        return self in {self.AUTOMATIC, self.CONVERT_AAC}

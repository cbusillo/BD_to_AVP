VIDEO_QUALITY_MAPPING_VERSION = 2

QUALITY_STEP_IDS = (
    "space_saver",
    "compact",
    "efficient",
    "balanced",
    "detailed",
    "high_detail",
    "maximum_detail",
)

DIRECT_QUALITY_BY_STEP = {
    "space_saver": 0.4,
    "compact": 0.5,
    "efficient": 0.6,
    "balanced": 0.7,
    "detailed": 0.75,
    "high_detail": 0.8,
    "maximum_detail": 0.85,
}

DIRECT_METALFX_2X_QUALITY_BY_STEP = {
    "space_saver": 0.3,
    "compact": 0.4,
    "efficient": 0.5,
    "balanced": 0.6,
    "detailed": 0.65,
    "high_detail": 0.7,
    "maximum_detail": 0.75,
}

GENERATED_QUALITY_BY_STEP = {
    "balanced": {
        "eye_bitrate_mbps": 20,
        "merge_quality": 75,
    },
}

FILE_UPSCALE_QUALITY_BY_STEP = {
    "balanced": 75,
    "detailed": 100,
}

AUTOMATIC_DIRECT_QUALITY = 0.7
AUTOMATIC_DIRECT_UPSCALE_QUALITY = 0.6
AUTOMATIC_GENERATED_EYE_BITRATE_MBPS = 20
AUTOMATIC_GENERATED_MERGE_QUALITY = 75
DEFAULT_AV1_CRF = 32
DEFAULT_UPSCALE_QUALITY = 75

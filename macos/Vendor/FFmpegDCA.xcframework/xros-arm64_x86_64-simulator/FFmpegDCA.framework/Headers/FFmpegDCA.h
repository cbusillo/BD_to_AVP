// FFmpegDCA: a narrow C interface to FFmpeg's LGPL `dca` decoder, configured to decode the DTS
// core only. The framework links nothing else from FFmpeg, and it is built without any GPL
// component. See vendor/ffmpeg-dca-visionos.toml and scripts/build_ffmpeg_dca_visionos.py.

#ifndef FFMPEG_DCA_H
#define FFMPEG_DCA_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct BDAVPDCADecoder BDAVPDCADecoder;

/// Decoded output of one DTS core frame, interleaved 32-bit float in FFmpeg's native channel
/// order (the bit order of `channel_mask`, which uses FFmpeg's AV_CH_* bits).
typedef struct BDAVPDCAFrameInfo {
    int32_t samples_per_channel;
    int32_t channels;
    int32_t sample_rate;
    uint64_t channel_mask;
} BDAVPDCAFrameInfo;

/// Creates a decoder with FFmpeg's `core_only` option set. Returns NULL on failure.
BDAVPDCADecoder *bdavp_dca_create(void);

void bdavp_dca_destroy(BDAVPDCADecoder *decoder);

/// Decodes one DTS core frame. `output` receives up to `output_capacity` interleaved floats.
/// Returns 0 on success, a positive value when the frame produced no audio, or a negative
/// FFmpeg error code.
int32_t bdavp_dca_decode(
    BDAVPDCADecoder *decoder,
    const uint8_t *data,
    int32_t size,
    float *output,
    int32_t output_capacity,
    BDAVPDCAFrameInfo *info
);

/// Drops decoder state, for a seek or a track switch.
void bdavp_dca_flush(BDAVPDCADecoder *decoder);

/// FFmpeg's version string, for example "9.0.2".
const char *bdavp_dca_ffmpeg_version(void);

/// libavcodec's license string, for example "LGPL version 2.1 or later".
const char *bdavp_dca_license(void);

#ifdef __cplusplus
}
#endif

#endif

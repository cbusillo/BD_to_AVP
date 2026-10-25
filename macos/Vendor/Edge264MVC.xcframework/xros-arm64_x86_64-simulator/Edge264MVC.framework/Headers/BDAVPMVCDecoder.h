// Edge264MVC: a narrow C interface to the edge264-mvc H.264 MVC decoder for Shiny 3D Cinema. It
// decodes Blu-ray 3D access units into stereo frames and copies each view into an NV12 pixel
// buffer. See vendor/edge264-mvc-visionos.toml and scripts/build_edge264_visionos.py.

#ifndef BDAVP_MVC_DECODER_H
#define BDAVP_MVC_DECODER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct BDAVPMVCDecoder BDAVPMVCDecoder;

enum {
    BDAVP_MVC_OK = 0,
    /// The frame callback asked to stop.
    BDAVP_MVC_STOPPED = 1,
    BDAVP_MVC_INVALID = -1,
    BDAVP_MVC_NO_MEMORY = -2,
    /// The stream is not 8-bit 4:2:0, which every Blu-ray 3D title is.
    BDAVP_MVC_UNSUPPORTED_FORMAT = -3,
    /// The decoder stopped making progress.
    BDAVP_MVC_STALLED = -4,
};

enum {
    BDAVP_MVC_BASE_CONCEALED = 1 << 0,
    BDAVP_MVC_DEPENDENT_CONCEALED = 1 << 1,
    /// The frame has no dependent view; both views show the base view.
    BDAVP_MVC_DEPENDENT_MISSING = 1 << 2,
    BDAVP_MVC_RANDOM_ACCESS = 1 << 3,
};

/// One stereo frame in display order. The planes stay valid only during the callback.
typedef struct BDAVPMVCFrame {
    /// [view][Y, Cb, Cr]; view 0 is the MVC base view.
    const uint8_t *planes[2][3];
    int32_t width;
    int32_t height;
    int32_t chroma_width;
    int32_t chroma_height;
    int32_t luma_stride;
    int32_t chroma_stride;
    /// The value passed to `bdavp_mvc_decode` with the access unit that coded this frame.
    int64_t pts;
    uint32_t flags;
} BDAVPMVCFrame;

typedef struct BDAVPMVCStatistics {
    uint64_t frames;
    uint64_t frames_without_dependent;
    uint64_t corrupt_units;
    uint64_t unsupported_units;
} BDAVPMVCStatistics;

/// Returns nonzero to stop decoding.
typedef int (*bdavp_mvc_frame_fn)(void *context, const BDAVPMVCFrame *frame);

/// Opens a decoder. `threads` follows edge264: 0 picks a count from the CPUs, 1 decodes on the
/// calling thread. Returns NULL on failure.
BDAVPMVCDecoder *bdavp_mvc_open(int32_t threads);

void bdavp_mvc_close(BDAVPMVCDecoder *decoder);

/// Decodes one access unit, one or more Annex B NAL units, and calls `emit` for every frame that
/// becomes ready, in display order. `pts` is returned with the frames this unit codes.
int32_t bdavp_mvc_decode(
    BDAVPMVCDecoder *decoder,
    const uint8_t *data,
    size_t size,
    int64_t pts,
    bdavp_mvc_frame_fn emit,
    void *context
);

/// Ends the stream and calls `emit` for every frame still held.
int32_t bdavp_mvc_finish(BDAVPMVCDecoder *decoder, bdavp_mvc_frame_fn emit, void *context);

/// Drops every held picture, for a seek. Decoding restarts at the next IDR.
void bdavp_mvc_flush(BDAVPMVCDecoder *decoder);

BDAVPMVCStatistics bdavp_mvc_statistics(const BDAVPMVCDecoder *decoder);

/// Copies one view into NV12 (a luma plane and an interleaved CbCr plane).
void bdavp_mvc_copy_nv12(
    const BDAVPMVCFrame *frame,
    int32_t view,
    uint8_t *luma,
    size_t luma_stride,
    uint8_t *chroma,
    size_t chroma_stride
);

/// edge264-mvc's version, for example "v2026.10.17".
const char *bdavp_mvc_version(void);

#ifdef __cplusplus
}
#endif

#endif

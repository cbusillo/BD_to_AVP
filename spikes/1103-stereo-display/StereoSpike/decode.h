// Throwaway spike for #1103: decode an in-memory Blu-ray 3D MVC stream with
// edge264-mvc and hand each stereo frame to a callback that presents it.
#ifndef STEREO_SPIKE_DECODE_H
#define STEREO_SPIKE_DECODE_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    const uint8_t *planes[2][3]; // [view][Y, Cb, Cr]; view 0 is the MVC base view
    int32_t width, height, width_c, height_c, stride_y, stride_c;
    int32_t concealed; // bit 0: base view concealed, bit 1: dependent view concealed
} SpikeFrame;

typedef struct {
    int64_t frames;
    int64_t mono_frames; // frames missing the dependent view
    int64_t corrupt_nals;
    int64_t unsupported_nals;
    int32_t result;
} DecodeStats;

// Returns nonzero to stop decoding. Called on the decoding thread; the planes
// stay valid only during the call.
typedef int (*spike_frame_cb)(void *ctx, const SpikeFrame *frame);

// Decodes buf `loops` times (loops <= 0 means until the callback stops it).
int decode_run(const uint8_t *buf, size_t size, int n_threads, int loops, spike_frame_cb cb, void *ctx,
               volatile DecodeStats *stats);

// Copies one planar 4:2:0 view into an NV12 destination and burns an eye
// marker ('L' or 'R') into it, with the low 16 bits of frame_index as a row
// of black and white cells under the glyph, and mode_label ('A', 'B' or 'C')
// top right at the same place in both eyes. Pass 0 for no marker or label.
void spike_copy_nv12(const SpikeFrame *f, int view, uint8_t *dst_y, size_t dst_y_stride, uint8_t *dst_uv,
                     size_t dst_uv_stride, char marker, int64_t frame_index, char mode_label);

// Reads the frame index cells back from a displayed luma plane. Returns -1
// when the guard cells don't match. *marker_on is set when the glyph is 'R'.
int32_t spike_read_index(const uint8_t *y, size_t stride, int32_t *marker_on);

const char *spike_decoder_version(void);

#endif

// Edge264MVC wrapper; see BDAVPMVCDecoder.h. The NAL loop follows edge264's own test program: a NAL
// the decoder cannot take yet is sent again after the ready frames are received, and corrupt or
// unsupported NALs are skipped (Blu-ray 3D carries type 24 units edge264 does not use).
#include "BDAVPMVCDecoder.h"
#include "edge264mvc.h"

#include <stdlib.h>
#include <string.h>

/// Rounds without a returned frame before a NAL counts as stuck. edge264 promises progress on
/// every round, so this only guards against a decoder bug turning into a hang.
#define STALL_LIMIT 64

struct BDAVPMVCDecoder {
    Edge264MvcDecoder *decoder;
    BDAVPMVCStatistics statistics;
};

BDAVPMVCDecoder *bdavp_mvc_open(int32_t threads) {
    Edge264MvcSettings settings;
    edge264mvc_default_settings(&settings);
    settings.n_threads = threads;
    BDAVPMVCDecoder *wrapper = calloc(1, sizeof(*wrapper));
    if (!wrapper) return NULL;
    if (edge264mvc_open(&wrapper->decoder, &settings) != EDGE264MVC_OK) {
        free(wrapper);
        return NULL;
    }
    return wrapper;
}

void bdavp_mvc_close(BDAVPMVCDecoder *decoder) {
    if (!decoder) return;
    edge264mvc_close(&decoder->decoder);
    free(decoder);
}

/// Receives every ready frame, counting them in `*delivered`. Returns BDAVP_MVC_OK,
/// BDAVP_MVC_STOPPED or an error; `*ended` is set once the decoder has returned its last frame.
static int32_t drain(BDAVPMVCDecoder *decoder, bdavp_mvc_frame_fn emit, void *context, int *delivered, int *ended) {
    *delivered = 0;
    for (;;) {
        Edge264MvcFrame out;
        int result = edge264mvc_receive_frame(decoder->decoder, &out);
        if (result == EDGE264MVC_END) {
            *ended = 1;
            return BDAVP_MVC_OK;
        }
        if (result != EDGE264MVC_OK) return BDAVP_MVC_OK;
        if (out.bit_depth_Y != 8 || out.bit_depth_C != 8 ||
            out.width_C * 2 != out.width_Y || out.height_C * 2 != out.height_Y) {
            edge264mvc_release_frame(decoder->decoder, &out);
            return BDAVP_MVC_UNSUPPORTED_FORMAT;
        }
        BDAVPMVCFrame frame = {0};
        const Edge264MvcView *base = &out.views[0];
        const Edge264MvcView *dependent = out.views[1].planes[0] ? &out.views[1] : NULL;
        for (int plane = 0; plane < 3; plane++) {
            frame.planes[0][plane] = base->planes[plane];
            frame.planes[1][plane] = dependent ? dependent->planes[plane] : base->planes[plane];
        }
        frame.width = out.width_Y;
        frame.height = out.height_Y;
        frame.chroma_width = out.width_C;
        frame.chroma_height = out.height_C;
        frame.luma_stride = out.stride_Y;
        frame.chroma_stride = out.stride_C;
        frame.pts = base->pts;
        if (base->flags & EDGE264MVC_VIEW_CONCEALED) frame.flags |= BDAVP_MVC_BASE_CONCEALED;
        if (base->flags & EDGE264MVC_VIEW_IDR) frame.flags |= BDAVP_MVC_RANDOM_ACCESS;
        if (!dependent) {
            frame.flags |= BDAVP_MVC_DEPENDENT_MISSING;
            decoder->statistics.frames_without_dependent++;
        } else if (dependent->flags & EDGE264MVC_VIEW_CONCEALED) {
            frame.flags |= BDAVP_MVC_DEPENDENT_CONCEALED;
        }
        decoder->statistics.frames++;
        int stop = emit(context, &frame);
        edge264mvc_release_frame(decoder->decoder, &out);
        if (stop) return BDAVP_MVC_STOPPED;
        (*delivered)++;
    }
}

/// The offset of the next start code at or after `start`, including a leading zero byte, or
/// `size` when there is none. `*prefix` receives the start code's length.
static size_t next_start_code(const uint8_t *data, size_t start, size_t size, size_t *prefix) {
    if (start >= size) return size;
    size_t found = start + edge264mvc_find_start_code(data + start, size - start);
    if (found + 3 > size) return size;
    if (found > start && data[found - 1] == 0) {
        found--;
        *prefix = 4;
    } else {
        *prefix = 3;
    }
    return found;
}

static int32_t send_nal(
    BDAVPMVCDecoder *decoder,
    const uint8_t *nal,
    size_t size,
    int64_t pts,
    bdavp_mvc_frame_fn emit,
    void *context
) {
    for (int stalled = 0;;) {
        int result = edge264mvc_send_nal(decoder->decoder, nal, size, pts, 0);
        int delivered = 0, ended = 0;
        int32_t status = drain(decoder, emit, context, &delivered, &ended);
        if (status != BDAVP_MVC_OK) return status;
        switch (result) {
        case EDGE264MVC_OK: return BDAVP_MVC_OK;
        case EDGE264MVC_CORRUPT: decoder->statistics.corrupt_units++; return BDAVP_MVC_OK;
        case EDGE264MVC_UNSUPPORTED: decoder->statistics.unsupported_units++; return BDAVP_MVC_OK;
        case EDGE264MVC_NOMEM: return BDAVP_MVC_NO_MEMORY;
        case EDGE264MVC_AGAIN:
            stalled = delivered > 0 ? 0 : stalled + 1;
            if (stalled > STALL_LIMIT) return BDAVP_MVC_STALLED;
            break;
        default: return BDAVP_MVC_INVALID;
        }
    }
}

int32_t bdavp_mvc_decode(
    BDAVPMVCDecoder *decoder,
    const uint8_t *data,
    size_t size,
    int64_t pts,
    bdavp_mvc_frame_fn emit,
    void *context
) {
    if (!decoder || !emit || (!data && size)) return BDAVP_MVC_INVALID;
    size_t prefix = 0;
    size_t position = next_start_code(data, 0, size, &prefix);
    while (position < size) {
        size_t start = position + prefix;
        size_t end = next_start_code(data, start, size, &prefix);
        if (end > start) {
            int32_t result = send_nal(decoder, data + start, end - start, pts, emit, context);
            if (result != BDAVP_MVC_OK) return result;
        }
        position = end;
    }
    return BDAVP_MVC_OK;
}

int32_t bdavp_mvc_finish(BDAVPMVCDecoder *decoder, bdavp_mvc_frame_fn emit, void *context) {
    if (!decoder || !emit) return BDAVP_MVC_INVALID;
    edge264mvc_send_end(decoder->decoder);
    for (int stalled = 0, ended = 0; !ended;) {
        int delivered = 0;
        int32_t status = drain(decoder, emit, context, &delivered, &ended);
        if (status != BDAVP_MVC_OK) return status;
        stalled = delivered > 0 ? 0 : stalled + 1;
        if (!ended && stalled > STALL_LIMIT) return BDAVP_MVC_STALLED;
    }
    return BDAVP_MVC_OK;
}

void bdavp_mvc_flush(BDAVPMVCDecoder *decoder) {
    if (decoder) edge264mvc_flush(decoder->decoder);
}

BDAVPMVCStatistics bdavp_mvc_statistics(const BDAVPMVCDecoder *decoder) {
    BDAVPMVCStatistics empty = {0};
    return decoder ? decoder->statistics : empty;
}

void bdavp_mvc_copy_nv12(
    const BDAVPMVCFrame *frame,
    int32_t view,
    uint8_t *luma,
    size_t luma_stride,
    uint8_t *chroma,
    size_t chroma_stride
) {
    if (!frame || view < 0 || view > 1 || !luma || !chroma) return;
    const uint8_t *y = frame->planes[view][0];
    const uint8_t *cb = frame->planes[view][1];
    const uint8_t *cr = frame->planes[view][2];
    for (int32_t row = 0; row < frame->height; row++) {
        memcpy(luma + (size_t)row * luma_stride, y + (size_t)row * (size_t)frame->luma_stride, (size_t)frame->width);
    }
    for (int32_t row = 0; row < frame->chroma_height; row++) {
        const uint8_t *u = cb + (size_t)row * (size_t)frame->chroma_stride;
        const uint8_t *v = cr + (size_t)row * (size_t)frame->chroma_stride;
        uint8_t *out = chroma + (size_t)row * chroma_stride;
        for (int32_t column = 0; column < frame->chroma_width; column++) {
            out[2 * column] = u[column];
            out[2 * column + 1] = v[column];
        }
    }
}

const char *bdavp_mvc_version(void) { return edge264mvc_version(); }

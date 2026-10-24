// Throwaway spike for #1103. The NAL loop mirrors #1101's bench.c (skip
// unsupported/corrupt NALs, hold one frame, resend on AGAIN).
#include "decode.h"
#include "edge264mvc.h"

#include <stdlib.h>
#include <string.h>

const char *spike_decoder_version(void) { return edge264mvc_version(); }

static size_t next_start(const uint8_t *data, size_t start, size_t end, size_t *delimiter) {
    if (start >= end) return SIZE_MAX;
    const uint8_t *found = data + start + edge264mvc_find_start_code(data + start, end - start);
    if (found == data + end) return SIZE_MAX;
    if (found > data + start && found[-1] == 0) found--;
    if (delimiter) *delimiter = found[2] == 1 ? 3 : 4;
    return (size_t)(found - data);
}

typedef struct {
    Edge264MvcDecoder *d;
    Edge264MvcFrame out;
    int holding;
    int stop;
    spike_frame_cb cb;
    void *ctx;
    volatile DecodeStats *s;
} Ctx;

static void release(Ctx *c) {
    if (c->holding) {
        edge264mvc_release_frame(c->d, &c->out);
        c->holding = 0;
    }
}

static void consume(Ctx *c) {
    Edge264MvcFrame *f = &c->out;
    SpikeFrame sf = {0};
    for (int v = 0; v < 2; v++) {
        // A frame without the dependent view repeats the base view in both eyes.
        const Edge264MvcView *view = f->views[v].planes[0] ? &f->views[v] : &f->views[0];
        for (int p = 0; p < 3; p++) sf.planes[v][p] = view->planes[p];
        if (view->flags & EDGE264MVC_VIEW_CONCEALED) sf.concealed |= 1 << v;
    }
    if (!f->views[1].planes[0]) c->s->mono_frames++;
    sf.width = f->width_Y;
    sf.height = f->height_Y;
    sf.width_c = f->width_C;
    sf.height_c = f->height_C;
    sf.stride_y = f->stride_Y;
    sf.stride_c = f->stride_C;
    c->s->frames++;
    if (c->cb(c->ctx, &sf)) c->stop = 1;
}

static int drain(Ctx *c) {
    int drained = 0;
    while (!c->stop) {
        release(c);
        int r = edge264mvc_receive_frame(c->d, &c->out);
        if (r != EDGE264MVC_OK) return r == EDGE264MVC_END ? -1 - drained : drained;
        c->holding = 1;
        consume(c);
        drained++;
    }
    return drained;
}

int decode_run(const uint8_t *buf, size_t size, int n_threads, int loops, spike_frame_cb cb, void *ctx,
               volatile DecodeStats *stats) {
    Edge264MvcSettings settings;
    edge264mvc_default_settings(&settings);
    settings.n_threads = n_threads;
    Ctx c = {0};
    c.cb = cb;
    c.ctx = ctx;
    c.s = stats;
    if (edge264mvc_open(&c.d, &settings) != EDGE264MVC_OK) return stats->result = -100;
    for (int loop = 0; (loops <= 0 || loop < loops) && !c.stop; loop++) {
        size_t delimiter = 0;
        size_t pos = next_start(buf, 0, size, &delimiter);
        if (pos == SIZE_MAX) { stats->result = -101; break; }
        pos += delimiter;
        int stuck = 0;
        while (!c.stop) {
            size_t next = next_start(buf, pos, size, &delimiter);
            size_t end = next == SIZE_MAX ? size : next;
            release(&c);
            int r = edge264mvc_send_nal(c.d, buf + pos, end - pos, 0, 0);
            int drained = drain(&c);
            if (r == EDGE264MVC_AGAIN) {
                stuck = drained == 0 ? stuck + 1 : 0;
                if (stuck > 64) { stats->result = -102; goto finish; }
                continue;
            }
            stuck = 0;
            if (r == EDGE264MVC_CORRUPT) stats->corrupt_nals++;
            else if (r == EDGE264MVC_UNSUPPORTED) stats->unsupported_nals++;
            else if (r != EDGE264MVC_OK) { stats->result = r; goto finish; }
            if (next == SIZE_MAX) break;
            pos = next + delimiter;
        }
        release(&c);
        edge264mvc_send_end(c.d);
        while (!c.stop && drain(&c) >= 0) {}
        // Restart cleanly for the next loop so the stream starts at its first IDR.
        release(&c);
        edge264mvc_close(&c.d);
        if (edge264mvc_open(&c.d, &settings) != EDGE264MVC_OK) { stats->result = -103; return stats->result; }
    }
finish:
    release(&c);
    edge264mvc_close(&c.d);
    return stats->result;
}

#define MARK_SCALE 28
#define MARK_PAD 24
#define MARK_X 72
#define MARK_Y 72
#define CELL 9
#define CELLS 18 // white guard, 16 index bits (LSB first), black guard
#define CELL_X (MARK_X + 13)
#define CELL_Y (MARK_Y + 7 * MARK_SCALE + MARK_PAD + 6)

// 5x7 glyphs, one byte per row, bit 4 is the leftmost column.
static const uint8_t GLYPH_L[7] = {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f};
static const uint8_t GLYPH_R[7] = {0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11};
static const uint8_t GLYPH_A[7] = {0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11};
static const uint8_t GLYPH_B[7] = {0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e};
static const uint8_t GLYPH_C[7] = {0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e};

static const uint8_t *glyph_for(char c) {
    switch (c) {
    case 'L': return GLYPH_L;
    case 'R': return GLYPH_R;
    case 'A': return GLYPH_A;
    case 'B': return GLYPH_B;
    default: return GLYPH_C;
    }
}

// A black box with a white 5x7 glyph, neutral chroma, top-left corner at (x0, y0).
static void draw_glyph(const SpikeFrame *f, uint8_t *dst_y, size_t dst_y_stride, uint8_t *dst_uv, size_t dst_uv_stride,
                       const uint8_t *glyph, int x0, int y0) {
    const int scale = MARK_SCALE, pad = MARK_PAD;
    const int box_w = 5 * scale + 2 * pad, box_h = 7 * scale + 2 * pad;
    for (int row = 0; row < box_h && y0 + row < f->height; row++) {
        uint8_t *line = dst_y + (size_t)(y0 + row) * dst_y_stride;
        int gy = (row - pad) / scale, in_y = row >= pad && gy < 7;
        for (int col = 0; col < box_w && x0 + col < f->width; col++) {
            int gx = (col - pad) / scale;
            int on = in_y && col >= pad && gx < 5 && (glyph[gy] >> (4 - gx) & 1);
            line[x0 + col] = on ? 235 : 16;
        }
    }
    for (int row = y0 / 2; row < (y0 + box_h) / 2 && row < f->height_c; row++) {
        uint8_t *line = dst_uv + (size_t)row * dst_uv_stride;
        for (int col = x0 / 2; col < (x0 + box_w) / 2 && col < f->width_c; col++) line[2 * col] = line[2 * col + 1] = 128;
    }
}

void spike_copy_nv12(const SpikeFrame *f, int view, uint8_t *dst_y, size_t dst_y_stride, uint8_t *dst_uv,
                     size_t dst_uv_stride, char marker, int64_t frame_index, char mode_label) {
    const uint8_t *y = f->planes[view][0], *cb = f->planes[view][1], *cr = f->planes[view][2];
    for (int row = 0; row < f->height; row++)
        memcpy(dst_y + (size_t)row * dst_y_stride, y + (size_t)row * f->stride_y, (size_t)f->width);
    for (int row = 0; row < f->height_c; row++) {
        const uint8_t *u = cb + (size_t)row * f->stride_c, *v = cr + (size_t)row * f->stride_c;
        uint8_t *d = dst_uv + (size_t)row * dst_uv_stride;
        for (int col = 0; col < f->width_c; col++) {
            d[2 * col] = u[col];
            d[2 * col + 1] = v[col];
        }
    }
    if (mode_label) draw_glyph(f, dst_y, dst_y_stride, dst_uv, dst_uv_stride, glyph_for(mode_label),
                               f->width - MARK_X - (5 * MARK_SCALE + 2 * MARK_PAD), MARK_Y);
    if (!marker) return;
    // A black box with a white glyph, at the same place in each eye, top left.
    draw_glyph(f, dst_y, dst_y_stride, dst_uv, dst_uv_stride, glyph_for(marker), MARK_X, MARK_Y);
    for (int cell = 0; cell < CELLS; cell++) {
        int on = cell == 0 ? 1 : cell == CELLS - 1 ? 0 : (int)(frame_index >> (cell - 1) & 1);
        for (int row = 0; row < CELL; row++)
            memset(dst_y + (size_t)(CELL_Y + row) * dst_y_stride + CELL_X + cell * CELL + 1, on ? 235 : 16, CELL - 2);
    }
}

int32_t spike_read_index(const uint8_t *y, size_t stride, int32_t *marker_on) {
    const uint8_t *row = y + (size_t)(CELL_Y + CELL / 2) * stride;
    int bits[CELLS];
    for (int cell = 0; cell < CELLS; cell++) bits[cell] = row[CELL_X + cell * CELL + CELL / 2] > 128;
    // Glyph row 0, column 3: clear for 'L' (0x10), set for 'R' (0x1e).
    const uint8_t *glyph_row = y + (size_t)(MARK_Y + MARK_PAD + MARK_SCALE / 2) * stride;
    *marker_on = glyph_row[MARK_X + MARK_PAD + 3 * MARK_SCALE + MARK_SCALE / 2] > 128;
    if (!bits[0] || bits[CELLS - 1]) return -1;
    int32_t index = 0;
    for (int b = 0; b < 16; b++) index |= bits[b + 1] << b;
    return index;
}

// Throwaway spike for #1101. Mirrors edge264mvc_test's mapped-input loop
// (skip unsupported/corrupt NALs, hold one frame, retry on AGAIN).
#include "bench.h"
#include "edge264mvc.h"

#include <mach/mach_time.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now_seconds(void) {
    static mach_timebase_info_data_t tb;
    if (tb.denom == 0) mach_timebase_info(&tb);
    return (double)mach_absolute_time() * tb.numer / tb.denom / 1e9;
}

static void sleep_until(double t) {
    for (;;) {
        double remaining = t - now_seconds();
        if (remaining <= 0) return;
        struct timespec ts = {(time_t)remaining, (long)((remaining - (time_t)remaining) * 1e9)};
        nanosleep(&ts, NULL);
    }
}

static size_t next_start(const uint8_t *data, size_t start, size_t end, size_t *delimiter) {
    if (start >= end) return SIZE_MAX;
    const uint8_t *found = data + start + edge264mvc_find_start_code(data + start, end - start);
    if (found == data + end) return SIZE_MAX;
    if (found > data + start && found[-1] == 0) found--;
    if (delimiter) *delimiter = found[2] == 1 ? 3 : 4;
    return (size_t)(found - data);
}

const char *bench_decoder_version(void) { return edge264mvc_version(); }

typedef struct {
    Edge264MvcDecoder *d;
    Edge264MvcFrame out;
    int holding;
    uint8_t *copy_buf;
    size_t copy_cap;
    int copy_frames;
    double pace_fps;
    double t0;
    double pace_start;
    double *frame_times;
    size_t cap;
    volatile int64_t *progress;
    BenchStats *s;
} Ctx;

static void release(Ctx *c) {
    if (c->holding) {
        edge264mvc_release_frame(c->d, &c->out);
        c->holding = 0;
    }
}

static void consume(Ctx *c) {
    Edge264MvcFrame *f = &c->out;
    double t = now_seconds();
    int64_t i = c->s->frames;
    if ((size_t)i < c->cap) c->frame_times[i] = t - c->t0;
    c->s->width = f->width_Y;
    c->s->height = f->height_Y;
    if (f->views[0].planes[0] && f->views[1].planes[0]) c->s->stereo_frames++;
    for (int v = 0; v < 2; v++)
        if (f->views[v].planes[0] && (f->views[v].flags & EDGE264MVC_VIEW_CONCEALED)) c->s->concealed_views++;
    if (c->copy_frames) {
        size_t y = (size_t)f->stride_Y * f->height_Y, uv = (size_t)f->stride_C * f->height_C;
        size_t need = 2 * (y + 2 * uv);
        if (need > c->copy_cap) {
            free(c->copy_buf);
            c->copy_buf = malloc(need);
            c->copy_cap = need;
        }
        uint8_t *p = c->copy_buf;
        for (int v = 0; v < 2; v++) {
            if (!f->views[v].planes[0]) continue;
            memcpy(p, f->views[v].planes[0], y); p += y;
            memcpy(p, f->views[v].planes[1], uv); p += uv;
            memcpy(p, f->views[v].planes[2], uv); p += uv;
        }
    }
    c->s->frames++;
    if (c->progress) *c->progress = c->s->frames;
    if (c->pace_fps > 0) {
        if (c->pace_start == 0) c->pace_start = t; // first frame defines the clock
        double deadline = c->pace_start + (double)i / c->pace_fps;
        double late = t - deadline;
        if (late > 0.0005) {
            c->s->late_frames++;
            if (late > c->s->max_lateness) c->s->max_lateness = late;
        }
        sleep_until(deadline);
    }
}

static int drain(Ctx *c) {
    int drained = 0;
    for (;;) {
        release(c);
        int r = edge264mvc_receive_frame(c->d, &c->out);
        if (r != EDGE264MVC_OK) return r == EDGE264MVC_END ? -1 - drained : drained;
        c->holding = 1;
        consume(c);
        drained++;
    }
}

int bench_run(const uint8_t *buf, size_t size, int n_threads, double pace_fps, int loops,
              int copy_frames, double *frame_times, size_t frame_times_capacity,
              volatile int64_t *progress, BenchStats *stats) {
    memset(stats, 0, sizeof(*stats));
    Edge264MvcSettings settings;
    edge264mvc_default_settings(&settings);
    settings.n_threads = n_threads;
    Ctx c = {0};
    c.copy_frames = copy_frames;
    c.pace_fps = pace_fps;
    c.frame_times = frame_times;
    c.cap = frame_times_capacity;
    c.progress = progress;
    c.s = stats;
    if (edge264mvc_open(&c.d, &settings) != EDGE264MVC_OK) {
        stats->result = -100;
        return stats->result;
    }
    c.t0 = now_seconds();
    for (int loop = 0; loop < loops; loop++) {
        size_t delimiter = 0;
        size_t pos = next_start(buf, 0, size, &delimiter);
        if (pos == SIZE_MAX) { stats->result = -101; break; }
        pos += delimiter;
        int stuck = 0;
        for (;;) {
            size_t next = next_start(buf, pos, size, &delimiter);
            size_t end = next == SIZE_MAX ? size : next;
            release(&c);
            int r = edge264mvc_send_nal(c.d, buf + pos, end - pos, 0, 0);
            int drained = drain(&c);
            if (r == EDGE264MVC_AGAIN) {
                stuck = drained == 0 ? stuck + 1 : 0;
                if (stuck > 64) { stats->result = -102; goto finish; }
                continue; // resend the same NAL
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
        while (drain(&c) >= 0) {}
    }
finish:
    release(&c);
    stats->wall_seconds = now_seconds() - c.t0;
    edge264mvc_close(&c.d);
    free(c.copy_buf);
    return stats->result;
}

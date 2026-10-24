// Throwaway spike for #1101: drive edge264-mvc over an in-memory Annex B MVC stream.
#ifndef MVC_SPIKE_BENCH_H
#define MVC_SPIKE_BENCH_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    int64_t frames;          // frames received
    int64_t stereo_frames;   // frames carrying both views
    int64_t concealed_views; // views flagged EDGE264MVC_VIEW_CONCEALED
    int64_t corrupt_nals;
    int64_t unsupported_nals;
    int64_t late_frames;     // paced mode: frames received after their deadline
    double max_lateness;     // paced mode: seconds
    double wall_seconds;
    int32_t width;
    int32_t height;
    int32_t result;          // 0 ok, negative on a decoder failure
} BenchStats;

// Decodes buf `loops` times. n_threads follows Edge264MvcSettings (0 = auto).
// pace_fps > 0 holds each frame until its presentation deadline. copy_frames
// copies both views' planes out (stand-in for a texture upload).
// frame_times receives the receive time of each frame in seconds since start.
// progress is updated with the frame count so the UI and sampler can watch it.
int bench_run(const uint8_t *buf, size_t size, int n_threads, double pace_fps, int loops,
              int copy_frames, double *frame_times, size_t frame_times_capacity,
              volatile int64_t *progress, BenchStats *stats);

const char *bench_decoder_version(void);

#endif

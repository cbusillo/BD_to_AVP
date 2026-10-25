// Blu-ray 3D source access for Shiny 3D Cinema: disc and title facts from libbluray, and a
// demuxer that pairs each MVC base-view access unit with its dependent view. The demuxer is
// adapted from native/ssif_probe, which the Mac relay uses until the headset path replaces it.
#ifndef BD3D_SOURCE_H
#define BD3D_SOURCE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BD3D_MAX_STREAMS 32
#define BD3D_MAX_SELECTED_PIDS 16
#define BD3D_BASE_VIEW_PID 0x1011
#define BD3D_DEPENDENT_VIEW_PID 0x1012

typedef enum {
    BD3D_OK = 0,
    BD3D_STOPPED = 1,
    BD3D_READ_FAILED = -1,
    BD3D_INVALID_PACKET = -3,
    BD3D_INVALID_PES = -4,
    BD3D_OUT_OF_MEMORY = -5,
    BD3D_PENDING_LIMIT = -6,
    BD3D_NON_MONOTONIC = -7,
    BD3D_UNMATCHED_PES = -8,
    BD3D_NO_MVC = -9,
    BD3D_TOO_MANY_PIDS = -10,
    BD3D_SSIF_UNAVAILABLE = -11,
} bd3d_result;

typedef struct {
    bool content_3d;
    bool aacs_detected;
    bool bdplus_detected;
    uint32_t title_count;
} bd3d_disc_info;

typedef struct {
    uint16_t pid;
    uint8_t coding_type;
    uint8_t format;
    uint8_t rate;
    char language[4];
} bd3d_stream;

typedef struct {
    uint32_t playlist;
    uint64_t duration_ticks;
    uint32_t angle_count;
    uint32_t clip_count;
    bool main_feature;
    bool complete_clip;
    bool ssif_available;
    bool base_view_right;
    char clip_id[8];
    uint8_t audio_count;
    uint8_t subtitle_count;
    bd3d_stream audio[BD3D_MAX_STREAMS];
    bd3d_stream subtitles[BD3D_MAX_STREAMS];
} bd3d_title;

typedef enum {
    BD3D_PACKET_VIDEO = 1,
    BD3D_PACKET_AUDIO = 2,
    BD3D_PACKET_SUBTITLE = 3,
} bd3d_packet_kind;

/// One demuxed packet. For video, `data` is the base view and `dependent` the dependent view of
/// one stereo access unit, both Annex B, with the PES timestamps. For audio and subtitles, `data`
/// is the whole PES packet, header included, and the timestamps are unset. The bytes are valid
/// only during the callback.
typedef struct {
    bd3d_packet_kind kind;
    uint16_t pid;
    bool random_access;
    uint64_t pts;
    uint64_t dts;
    const uint8_t *data;
    size_t length;
    const uint8_t *dependent;
    size_t dependent_length;
} bd3d_packet;

typedef struct {
    uint64_t bytes;
    uint64_t transport_packets;
    uint64_t video_pairs;
    uint64_t audio_packets;
    uint64_t subtitle_packets;
    size_t maximum_pending_bytes;
} bd3d_stats;

/// Returns nonzero to stop demuxing.
typedef int (*bd3d_packet_fn)(void *context, const bd3d_packet *packet);
/// Returns the bytes read, 0 at the end, or a negative value on failure. A failed read leaves
/// the stream where it was, so the same read can be tried again.
typedef int64_t (*bd3d_read_fn)(void *context, uint8_t *buffer, int64_t size);

typedef enum {
    BD3D_READ_EVENT_STARTED,
    BD3D_READ_EVENT_FAILED,
    BD3D_READ_EVENT_FINISHED,
} bd3d_read_event;

/// Watches each read, for a source on a network share. On BD3D_READ_EVENT_FAILED it may wait, then
/// returns nonzero to try the same read again or zero to give up. Its return value is ignored
/// for the other events.
typedef int (*bd3d_read_monitor_fn)(void *context, bd3d_read_event event);

typedef struct bd3d_disc bd3d_disc;

/// Opens an ISO image or a folder that contains BDMV. Returns NULL when libbluray cannot.
bd3d_disc *bd3d_disc_open(const char *path);
void bd3d_disc_close(bd3d_disc *disc);
bd3d_disc_info bd3d_disc_get_info(const bd3d_disc *disc);
/// Fills `title` for the index-th title of at least 60 seconds. Returns false when unavailable.
bool bd3d_disc_title(bd3d_disc *disc, uint32_t index, bd3d_title *title);
/// Demuxes the SSIF of a single-clip title. PIDs not listed are skipped. `monitor` may be NULL.
bd3d_result bd3d_disc_stream(
    bd3d_disc *disc,
    const char *clip_id,
    bd3d_read_monitor_fn monitor,
    void *monitor_context,
    const uint16_t *audio_pids,
    size_t audio_count,
    const uint16_t *subtitle_pids,
    size_t subtitle_count,
    bd3d_packet_fn emit,
    void *emit_context,
    bd3d_stats *stats
);
/// Demuxes a 192-byte-packet M2TS stream from any reader. `monitor` may be NULL.
bd3d_result bd3d_demux(
    bd3d_read_fn read,
    void *read_context,
    bd3d_read_monitor_fn monitor,
    void *monitor_context,
    const uint16_t *audio_pids,
    size_t audio_count,
    const uint16_t *subtitle_pids,
    size_t subtitle_count,
    bd3d_packet_fn emit,
    void *emit_context,
    bd3d_stats *stats
);

#endif

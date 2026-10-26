// Blu-ray 3D source access shared by Shiny 3D Cinema and the Mac converter (native/bd3d_reader):
// disc and title facts from libbluray, and a demuxer that pairs each MVC base-view access unit
// with its dependent view.
//
// A title is a playlist of clips. The reader plays them in order and rewrites every timestamp
// onto the playlist's own timeline, which starts at zero, so audio and video keep one clock
// across clip boundaries.
#ifndef BD3D_SOURCE_H
#define BD3D_SOURCE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BD3D_MAX_STREAMS 32
#define BD3D_MAX_SELECTED_PIDS 64
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
    BD3D_TITLE_UNAVAILABLE = -12,
    BD3D_INDEX_UNAVAILABLE = -13,
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
    /// The clips the title plays: the playlist's items up to its first non-seamless connection.
    uint32_t clip_count;
    bool main_feature;
    /// Every clip is played from a point where decoding can start, through its end or to a
    /// point where the next decodable picture starts, so the reader can join the clips.
    bool clips_readable;
    /// Every clip has its SSIF stereo file.
    bool ssif_available;
    bool base_view_right;
    /// The first clip.
    char clip_id[8];
    uint8_t video_count;
    bd3d_stream video;
    uint8_t audio_count;
    uint8_t subtitle_count;
    /// The first clip's streams, which the playlist keeps for every clip.
    bd3d_stream audio[BD3D_MAX_STREAMS];
    bd3d_stream subtitles[BD3D_MAX_STREAMS];
} bd3d_title;

typedef enum {
    BD3D_PACKET_VIDEO = 1,
    BD3D_PACKET_AUDIO = 2,
    BD3D_PACKET_SUBTITLE = 3,
    /// A playlist item's part of a clip starts, at `pts` on the playlist timeline, with PID 0 and
    /// no data. Sent only when subtitles are selected: subtitle packets outside each part are
    /// dropped, so a line whose clear falls past a clip's end must be cleared here.
    BD3D_PACKET_CLIP_START = 4,
} bd3d_packet_kind;

/// One demuxed packet. For video, `data` is the base view and `dependent` the dependent view of
/// one stereo access unit, both Annex B, with the PES timestamps. For audio and subtitles, `data`
/// is the whole PES packet, header included, and the timestamps are unset. The bytes are valid
/// only during the callback. A title's timestamps, including those inside audio and subtitle PES
/// headers, are on the playlist timeline.
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
    /// Bytes skipped to find the packet sync again, as across a damaged stretch of an image.
    uint64_t skipped_bytes;
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

/// Watches each read, for a source on a network share. On BD3D_READ_EVENT_FAILED, with the read's
/// errno as `error`, it may wait, then returns nonzero to try the same read again or zero to give
/// up. `error` is 0 and the return value is ignored for the other events.
typedef int (*bd3d_read_monitor_fn)(void *context, bd3d_read_event event, int error);

typedef struct bd3d_disc bd3d_disc;

/// Opens an ISO image or a folder that contains BDMV. Returns NULL when libbluray cannot.
bd3d_disc *bd3d_disc_open(const char *path);
void bd3d_disc_close(bd3d_disc *disc);
bd3d_disc_info bd3d_disc_get_info(const bd3d_disc *disc);
/// Fills `title` for the index-th title of at least 60 seconds. Returns false when unavailable.
bool bd3d_disc_title(bd3d_disc *disc, uint32_t index, bd3d_title *title);
/// Demuxes a title's clips in order from `start_ticks` on its timeline, rewriting every
/// timestamp onto that timeline. Video starts at the last random access unit at or before
/// `start_ticks`. PIDs not listed are skipped. `monitor` may be NULL.
bd3d_result bd3d_disc_stream(
    bd3d_disc *disc,
    uint32_t playlist,
    uint64_t start_ticks,
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

// MARK: - Clip segments and the clip index

/// The part of a clip that a playlist item plays, and where it lands on the playlist timeline.
/// Times are 90 kHz.
typedef struct {
    /// The clip PTS where the item starts.
    uint64_t in_time;
    /// The item's length; audio and subtitles outside [in_time, in_time + duration) are dropped.
    uint64_t duration;
    /// Where `in_time` lands on the playlist timeline. Every timestamp is rewritten to
    /// `timestamp - in_time + playlist_time`, modulo 2^33.
    uint64_t playlist_time;
    /// Video starts at the first random access unit at or after `in_time + start_offset`; the
    /// units before it are dropped, with any view whose pair was read before the start.
    int64_t start_offset;
    /// Stop at the first random access unit at or after `in_time + duration`, because the item
    /// ends before the clip does.
    bool trim_end;
} bd3d_segment;

/// Demuxes one playlist item's part of a clip, read from `read`, onto the playlist timeline.
/// `monitor` may be NULL.
bd3d_result bd3d_demux_segment(
    bd3d_read_fn read,
    void *read_context,
    const bd3d_segment *segment,
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

/// One entry of a clip's entry-point (EP) map: a random access unit's PTS, with the bits the
/// map omits cleared, and the source packet number where it starts in the clip's M2TS file.
typedef struct {
    uint64_t pts;
    uint32_t spn;
} bd3d_entry_point;

/// What a clip information (CLPI) file says about seeking in its clip: the EP map of one video
/// PID, and the extent start points that interleave it in the SSIF file.
typedef struct {
    /// Whether the clip has one ATC and one STC sequence, as the reader needs.
    bool single_sequence;
    /// The PTS range the clip presents, from its sequence info.
    uint64_t presentation_start;
    uint64_t presentation_end;
    uint32_t source_packets;
    bd3d_entry_point *entry_points;
    size_t entry_point_count;
    uint32_t *extent_starts;
    size_t extent_count;
} bd3d_clip_index;

/// Parses a CLPI file. Returns false when it is malformed or has no EP map for `pid`; a clip
/// without extent start points parses with `extent_count` 0.
bool bd3d_clip_index_parse(const uint8_t *clpi, size_t size, uint16_t pid, bd3d_clip_index *index);
void bd3d_clip_index_free(bd3d_clip_index *index);
/// The last entry point at or before `pts`, or the first when `pts` precedes every entry.
/// Returns NULL for an empty map.
const bd3d_entry_point *bd3d_entry_point_at(const bd3d_clip_index *index, uint64_t pts);
/// The SSIF byte offset of the extent pair holding base-view source packet `spn`: base extent i
/// and dependent extent i sit together, in either order, after every earlier extent of both.
/// Returns -1 when the extent tables do not match.
int64_t bd3d_ssif_offset(const bd3d_clip_index *base, const bd3d_clip_index *dependent, uint32_t spn);
/// How many play items, from the first, join seamlessly (connection condition 5 or 6). A title
/// ends at its first non-seamless connection, as MakeMKV reads it: what follows, such as a
/// distributor logo after the feature, is a separate title. Returns 0 for a malformed playlist.
uint32_t bd3d_seamless_items(const uint8_t *mpls, size_t size);
/// The dependent-view clip of a playlist item, from the playlist's stereo sub-path extension.
bool bd3d_dependent_clip_id(const uint8_t *mpls, size_t size, uint32_t play_item, char clip_id[6]);

#endif

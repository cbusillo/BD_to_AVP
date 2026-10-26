#include "bd3d_source.h"

#include <libbluray/bluray.h>
#include <libbluray/filesystem.h>
#include <libbluray/log_control.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define M2TS_PACKET_SIZE 192
#define READ_CHUNK_SIZE (6144 * 128)
#define MAX_PES_BYTES (16 * 1024 * 1024)
#define MAX_PENDING_BYTES (256 * 1024 * 1024)
/// Bytes without packet sync before a stream counts as unreadable.
#define MAX_UNSYNCED_BYTES (16 * 1024 * 1024)
#define TITLES_MINIMUM_SECONDS 60
#define TIMESTAMP_MASK ((UINT64_C(1) << 33) - 1)
/// The EP map stores PTS without its low 9 bits.
#define ENTRY_POINT_PRECISION 512
/// Internal: the segment reached its trimmed end.
#define SEGMENT_ENDED 2

struct bd3d_disc {
    BLURAY *bluray;
    bd3d_disc_info info;
    uint32_t main_playlist;
};

typedef struct {
    uint8_t *data;
    size_t length;
    size_t capacity;
    uint64_t pts;
    uint64_t dts;
    bool active;
} PesBuilder;

typedef struct PesItem {
    uint8_t *data;
    size_t length;
    size_t capacity;
    uint64_t pts;
    uint64_t dts;
    struct PesItem *next;
} PesItem;

typedef struct {
    PesItem *head;
    size_t count;
    size_t bytes;
} PesQueue;

typedef struct {
    uint16_t pid;
    bd3d_packet_kind kind;
    PesBuilder builder;
    /// Whether the last timed packet fell inside the segment; untimed packets follow it.
    bool inside;
} SelectedStream;

typedef struct {
    PesBuilder base_builder;
    PesBuilder dependent_builder;
    PesQueue base_pending;
    PesQueue dependent_pending;
    SelectedStream selected[BD3D_MAX_SELECTED_PIDS];
    size_t selected_count;
    /// NULL to deliver the stream as it is.
    const bd3d_segment *segment;
    /// Whether video delivery has started; until then units before the segment's start drop.
    bool started;
    /// The PTS of the random access unit delivery started at. Leading pictures after it in
    /// decode order, shown before it, reference pictures that were never read, so they drop.
    uint64_t start_pts;
    /// Set once sync was lost: from then on each pair drops the views before it whose partner was
    /// in the lost bytes, and views left unpaired at the end are not an error.
    bool drop_unmatched;
    uint64_t last_dts;
    bd3d_stats stats;
    bd3d_packet_fn emit;
    void *emit_context;
} Demux;

// MARK: - Byte reading

static uint32_t read_be32(const uint8_t *data) {
    return ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) | ((uint32_t)data[2] << 8) | data[3];
}

static uint16_t read_be16(const uint8_t *data) {
    return (uint16_t)((data[0] << 8) | data[1]);
}

/// Whether [offset, offset + length) lies inside a buffer of `size` bytes.
static bool in_bounds(size_t size, size_t offset, size_t length) {
    return offset <= size && length <= size - offset;
}

/// Finds an extension data block (`id1`/`id2`) of a CLPI or MPLS file whose extension data starts
/// at `extension_start`. Returns false when it is absent or malformed.
static bool find_extension(
    const uint8_t *data, size_t size, uint32_t extension_start, uint16_t id1, uint16_t id2, size_t *offset,
    size_t *length
) {
    if (extension_start == 0 || !in_bounds(size, extension_start, 12)) {
        return false;
    }
    const uint8_t *extension = data + extension_start;
    uint8_t entries = extension[11];
    for (uint8_t index = 0; index < entries; index++) {
        size_t entry = extension_start + 12 + (size_t)index * 12;
        if (!in_bounds(size, entry, 12)) {
            return false;
        }
        if (read_be16(data + entry) == id1 && read_be16(data + entry + 2) == id2) {
            size_t start = extension_start + (size_t)read_be32(data + entry + 4);
            size_t block = read_be32(data + entry + 8);
            if (!in_bounds(size, start, block)) {
                return false;
            }
            *offset = start;
            *length = block;
            return true;
        }
    }
    return false;
}

// MARK: - Clip index

static bool parse_sequence(const uint8_t *clpi, size_t size, bd3d_clip_index *index) {
    size_t start = read_be32(clpi + 8);
    if (!in_bounds(size, start, 6)) {
        return false;
    }
    uint8_t atc_count = clpi[start + 5];
    if (atc_count != 1 || !in_bounds(size, start + 6, 6)) {
        index->single_sequence = false;
        return true;
    }
    uint8_t stc_count = clpi[start + 10];
    if (stc_count != 1 || !in_bounds(size, start + 12, 14)) {
        index->single_sequence = false;
        return true;
    }
    index->single_sequence = true;
    index->presentation_start = (uint64_t)read_be32(clpi + start + 18) * 2;
    index->presentation_end = (uint64_t)read_be32(clpi + start + 22) * 2;
    return true;
}

static bool parse_entry_points(const uint8_t *clpi, size_t size, uint16_t pid, bd3d_clip_index *index) {
    size_t cpi = read_be32(clpi + 16);
    if (!in_bounds(size, cpi, 8)) {
        return false;
    }
    size_t map = cpi + 6;
    uint8_t stream_count = clpi[map + 1];
    for (uint8_t stream = 0; stream < stream_count; stream++) {
        size_t entry = map + 2 + (size_t)stream * 12;
        if (!in_bounds(size, entry, 12)) {
            return false;
        }
        if (read_be16(clpi + entry) != pid) {
            continue;
        }
        uint64_t counts = ((uint64_t)read_be16(clpi + entry + 2) << 32) | read_be32(clpi + entry + 4);
        size_t coarse_count = (size_t)((counts >> 18) & 0xffff);
        size_t fine_count = (size_t)(counts & 0x3ffff);
        size_t table = map + read_be32(clpi + entry + 8);
        if (coarse_count == 0 || fine_count == 0 || !in_bounds(size, table, 4)) {
            return false;
        }
        size_t coarse = table + 4;
        size_t fine = table + read_be32(clpi + table);
        if (!in_bounds(size, coarse, coarse_count * 8) || !in_bounds(size, fine, fine_count * 4)) {
            return false;
        }
        index->entry_points = calloc(fine_count, sizeof(*index->entry_points));
        if (index->entry_points == NULL) {
            return false;
        }
        size_t coarse_index = 0;
        for (size_t fine_index = 0; fine_index < fine_count; fine_index++) {
            while (coarse_index + 1 < coarse_count &&
                   (read_be32(clpi + coarse + (coarse_index + 1) * 8) >> 14) <= fine_index) {
                coarse_index++;
            }
            uint32_t coarse_word = read_be32(clpi + coarse + coarse_index * 8);
            uint32_t coarse_spn = read_be32(clpi + coarse + coarse_index * 8 + 4);
            uint32_t fine_word = read_be32(clpi + fine + fine_index * 4);
            uint64_t coarse_pts = (uint64_t)(coarse_word & 0x3ffe) << 19;
            uint64_t fine_pts = (uint64_t)((fine_word >> 17) & 0x7ff) << 9;
            index->entry_points[fine_index] = (bd3d_entry_point){
                .pts = (coarse_pts + fine_pts) & TIMESTAMP_MASK,
                .spn = (coarse_spn & ~UINT32_C(0x1ffff)) + (fine_word & 0x1ffff),
            };
        }
        index->entry_point_count = fine_count;
        return true;
    }
    return false;
}

static bool parse_extent_starts(const uint8_t *clpi, size_t size, bd3d_clip_index *index) {
    size_t offset;
    size_t length;
    if (!find_extension(clpi, size, read_be32(clpi + 24), 2, 4, &offset, &length)) {
        return true;
    }
    if (length < 8) {
        return false;
    }
    size_t count = read_be32(clpi + offset + 4);
    if (count == 0) {
        return true;
    }
    if (count > (length - 8) / 4) {
        return false;
    }
    index->extent_starts = calloc(count, sizeof(*index->extent_starts));
    if (index->extent_starts == NULL) {
        return false;
    }
    for (size_t point = 0; point < count; point++) {
        index->extent_starts[point] = read_be32(clpi + offset + 8 + point * 4);
    }
    index->extent_count = count;
    return true;
}

bool bd3d_clip_index_parse(const uint8_t *clpi, size_t size, uint16_t pid, bd3d_clip_index *index) {
    memset(index, 0, sizeof(*index));
    if (size < 40 || memcmp(clpi, "HDMV", 4) != 0) {
        return false;
    }
    size_t clip_info = 40;
    if (in_bounds(size, clip_info, 24)) {
        index->source_packets = read_be32(clpi + clip_info + 16);
    }
    bool parsed = parse_sequence(clpi, size, index) && parse_extent_starts(clpi, size, index) &&
                  (pid == 0 || parse_entry_points(clpi, size, pid, index));
    if (!parsed) {
        bd3d_clip_index_free(index);
    }
    return parsed;
}

void bd3d_clip_index_free(bd3d_clip_index *index) {
    free(index->entry_points);
    free(index->extent_starts);
    memset(index, 0, sizeof(*index));
}

const bd3d_entry_point *bd3d_entry_point_at(const bd3d_clip_index *index, uint64_t pts) {
    if (index->entry_point_count == 0) {
        return NULL;
    }
    size_t low = 0;
    size_t high = index->entry_point_count;
    while (high - low > 1) {
        size_t middle = low + (high - low) / 2;
        if (index->entry_points[middle].pts <= pts) {
            low = middle;
        } else {
            high = middle;
        }
    }
    return &index->entry_points[low];
}

int64_t bd3d_ssif_offset(const bd3d_clip_index *base, const bd3d_clip_index *dependent, uint32_t spn) {
    if (base->extent_count == 0 || base->extent_count != dependent->extent_count || base->extent_starts[0] != 0) {
        return -1;
    }
    size_t extent = 0;
    while (extent + 1 < base->extent_count && base->extent_starts[extent + 1] <= spn) {
        extent++;
    }
    uint64_t packets = (uint64_t)base->extent_starts[extent] + dependent->extent_starts[extent];
    return (int64_t)(packets * M2TS_PACKET_SIZE);
}

uint32_t bd3d_seamless_items(const uint8_t *mpls, size_t size) {
    if (size < 20 || memcmp(mpls, "MPLS", 4) != 0) {
        return 0;
    }
    size_t list = read_be32(mpls + 8);
    if (!in_bounds(size, list, 10)) {
        return 0;
    }
    uint16_t items = read_be16(mpls + list + 6);
    size_t cursor = list + 10;
    uint32_t joined = 0;
    for (uint16_t index = 0; index < items; index++) {
        if (!in_bounds(size, cursor, 13)) {
            return joined;
        }
        uint8_t condition = mpls[cursor + 12] & 0x0f;
        if (index > 0 && condition != 5 && condition != 6) {
            break;
        }
        joined++;
        cursor += 2 + read_be16(mpls + cursor);
    }
    return joined;
}

/// The play items of `playlist` that make its title, at least one.
static uint32_t title_clip_count(BLURAY *bluray, uint32_t playlist, uint32_t clip_count) {
    char path[64];
    snprintf(path, sizeof(path), "BDMV/PLAYLIST/%05u.mpls", playlist);
    void *data = NULL;
    int64_t size = 0;
    uint32_t joined = 0;
    if (bd_read_file(bluray, path, &data, &size) && data != NULL && size > 0) {
        joined = bd3d_seamless_items(data, (size_t)size);
    }
    free(data);
    return joined == 0 || joined > clip_count ? clip_count : joined;
}

bool bd3d_dependent_clip_id(const uint8_t *mpls, size_t size, uint32_t play_item, char clip_id[6]) {
    if (size < 20 || memcmp(mpls, "MPLS", 4) != 0) {
        return false;
    }
    size_t offset;
    size_t length;
    if (!find_extension(mpls, size, read_be32(mpls + 16), 2, 2, &offset, &length) || length < 6) {
        return false;
    }
    size_t end = offset + length;
    uint16_t sub_paths = read_be16(mpls + offset + 4);
    size_t cursor = offset + 6;
    for (uint16_t sub_path = 0; sub_path < sub_paths; sub_path++) {
        if (!in_bounds(end, cursor, 4)) {
            return false;
        }
        size_t next = cursor + 4 + read_be32(mpls + cursor);
        if (next > end || !in_bounds(end, cursor, 10)) {
            return false;
        }
        uint8_t type = mpls[cursor + 5];
        uint8_t items = mpls[cursor + 9];
        // Sub-path type 8 carries the stereo dependent view, one sub play item per play item.
        size_t item = cursor + 10;
        for (uint8_t index = 0; type == 8 && index < items; index++) {
            if (!in_bounds(next, item, 2)) {
                return false;
            }
            size_t item_length = read_be16(mpls + item);
            if (item_length < 28 || !in_bounds(next, item + 2, item_length)) {
                return false;
            }
            bool multiple_clips = (mpls[item + 2 + 12] & 0x10) != 0;
            uint16_t synchronized_item = read_be16(mpls + item + 2 + 22);
            if (synchronized_item == play_item && !multiple_clips) {
                memcpy(clip_id, mpls + item + 2, 5);
                clip_id[5] = '\0';
                return true;
            }
            item += 2 + item_length;
        }
        cursor = next;
    }
    return false;
}

// MARK: - Disc and titles

bd3d_disc *bd3d_disc_open(const char *path) {
    bd_set_debug_mask(0);
    BLURAY *bluray = bd_open(path, NULL);
    if (bluray == NULL) {
        return NULL;
    }
    const BLURAY_DISC_INFO *disc_info = bd_get_disc_info(bluray);
    if (disc_info == NULL) {
        bd_close(bluray);
        return NULL;
    }
    bd3d_disc *disc = calloc(1, sizeof(*disc));
    if (disc == NULL) {
        bd_close(bluray);
        return NULL;
    }
    disc->bluray = bluray;
    disc->info.content_3d = disc_info->content_exist_3D;
    disc->info.aacs_detected = disc_info->aacs_detected;
    disc->info.bdplus_detected = disc_info->bdplus_detected;
    disc->info.title_count = bd_get_titles(bluray, TITLES_ALL, TITLES_MINIMUM_SECONDS);
    int main_title = bd_get_main_title(bluray);
    if (main_title >= 0) {
        BLURAY_TITLE_INFO *title = bd_get_title_info(bluray, (uint32_t)main_title, 0);
        if (title != NULL) {
            disc->main_playlist = title->playlist;
            bd_free_title_info(title);
        }
    }
    return disc;
}

void bd3d_disc_close(bd3d_disc *disc) {
    if (disc == NULL) {
        return;
    }
    bd_close(disc->bluray);
    free(disc);
}

bd3d_disc_info bd3d_disc_get_info(const bd3d_disc *disc) {
    return disc->info;
}

static void ssif_path(char *buffer, size_t size, const char *clip_id) {
    snprintf(buffer, size, "BDMV/STREAM/SSIF/%s.ssif", clip_id);
}

static bool ssif_available(BLURAY *bluray, const char *clip_id) {
    char path[64];
    ssif_path(path, sizeof(path), clip_id);
    BD_FILE_H *file = bd_open_file_dec(bluray, path);
    if (file == NULL) {
        return false;
    }
    int64_t size = file->seek(file, 0, SEEK_END);
    file->close(file);
    return size > 0;
}

static bool read_clip_index(BLURAY *bluray, const char *clip_id, uint16_t pid, bd3d_clip_index *index) {
    char path[64];
    snprintf(path, sizeof(path), "BDMV/CLIPINF/%s.clpi", clip_id);
    void *data = NULL;
    int64_t size = 0;
    if (!bd_read_file(bluray, path, &data, &size) || data == NULL || size <= 0) {
        free(data);
        return false;
    }
    bool parsed = bd3d_clip_index_parse(data, (size_t)size, pid, index);
    free(data);
    return parsed;
}

/// Reads the base and dependent clip indexes of a playlist item, for seeking inside its SSIF.
static bool read_stereo_indexes(
    BLURAY *bluray, uint32_t playlist, uint32_t play_item, const char *clip_id, bd3d_clip_index *base,
    bd3d_clip_index *dependent
) {
    char path[64];
    snprintf(path, sizeof(path), "BDMV/PLAYLIST/%05u.mpls", playlist);
    void *data = NULL;
    int64_t size = 0;
    char dependent_id[6];
    bool found = bd_read_file(bluray, path, &data, &size) && data != NULL && size > 0 &&
                 bd3d_dependent_clip_id(data, (size_t)size, play_item, dependent_id);
    free(data);
    if (!found || !read_clip_index(bluray, clip_id, BD3D_BASE_VIEW_PID, base)) {
        return false;
    }
    if (!read_clip_index(bluray, dependent_id, 0, dependent)) {
        bd3d_clip_index_free(base);
        return false;
    }
    return true;
}

/// Whether an entry point starts within the EP map's precision of `pts`, so a random access
/// unit sits exactly there.
static bool entry_point_near(const bd3d_clip_index *index, uint64_t pts) {
    const bd3d_entry_point *entry = bd3d_entry_point_at(index, pts);
    return entry != NULL && entry->pts <= pts && pts - entry->pts < ENTRY_POINT_PRECISION;
}

/// Whether the reader can play this playlist item's part of its clip: it must start where the
/// clip does or at a random access unit, and end where the clip does or at a random access unit.
static bool clip_readable(BLURAY *bluray, uint32_t playlist, uint32_t play_item, const BLURAY_CLIP_INFO *clip) {
    bd3d_clip_index base;
    if (!read_clip_index(bluray, clip->clip_id, BD3D_BASE_VIEW_PID, &base)) {
        return false;
    }
    bool readable = base.single_sequence && clip->in_time >= base.presentation_start &&
                    clip->out_time <= base.presentation_end && clip->in_time < clip->out_time;
    bool starts_inside = clip->in_time > base.presentation_start;
    if (readable && starts_inside) {
        readable = entry_point_near(&base, clip->in_time);
    }
    if (readable && clip->out_time < base.presentation_end) {
        readable = entry_point_near(&base, clip->out_time);
    }
    bd3d_clip_index_free(&base);
    if (readable && starts_inside) {
        bd3d_clip_index stereo_base;
        bd3d_clip_index dependent;
        readable = read_stereo_indexes(bluray, playlist, play_item, clip->clip_id, &stereo_base, &dependent);
        if (readable) {
            readable = bd3d_ssif_offset(&stereo_base, &dependent, 0) >= 0;
            bd3d_clip_index_free(&stereo_base);
            bd3d_clip_index_free(&dependent);
        }
    }
    return readable;
}

static uint8_t copy_streams(bd3d_stream *destination, const BLURAY_STREAM_INFO *source, uint8_t count) {
    uint8_t copied = count < BD3D_MAX_STREAMS ? count : BD3D_MAX_STREAMS;
    for (uint8_t index = 0; index < copied; index++) {
        destination[index].pid = source[index].pid;
        destination[index].coding_type = source[index].coding_type;
        destination[index].format = source[index].format;
        destination[index].rate = source[index].rate;
        memcpy(destination[index].language, source[index].lang, 3);
        destination[index].language[3] = '\0';
    }
    return copied;
}

bool bd3d_disc_title(bd3d_disc *disc, uint32_t index, bd3d_title *title) {
    if (index >= disc->info.title_count) {
        return false;
    }
    BLURAY_TITLE_INFO *info = bd_get_title_info(disc->bluray, index, 0);
    if (info == NULL) {
        return false;
    }
    memset(title, 0, sizeof(*title));
    title->playlist = info->playlist;
    title->angle_count = info->angle_count;
    title->clip_count = info->clip_count == 0 ? 0 : title_clip_count(disc->bluray, info->playlist, info->clip_count);
    title->duration_ticks = info->duration;
    if (title->clip_count < info->clip_count) {
        const BLURAY_CLIP_INFO *last = &info->clips[title->clip_count - 1];
        title->duration_ticks = last->start_time + (last->out_time - last->in_time);
    }
    title->main_feature = info->playlist == disc->main_playlist;
    title->base_view_right = info->mvc_base_view_r_flag;
    if (info->clip_count >= 1) {
        const BLURAY_CLIP_INFO *clip = &info->clips[0];
        snprintf(title->clip_id, sizeof(title->clip_id), "%s", clip->clip_id);
        title->video_count = copy_streams(&title->video, clip->video_streams, clip->video_stream_count > 0 ? 1 : 0);
        title->audio_count = copy_streams(title->audio, clip->audio_streams, clip->audio_stream_count);
        title->subtitle_count = copy_streams(title->subtitles, clip->pg_streams, clip->pg_stream_count);
    }
    // The per-clip checks read files, so they run only for titles the structure allows.
    bool plausible = !disc->info.aacs_detected && !disc->info.bdplus_detected && disc->info.content_3d &&
                     info->angle_count == 1 && info->clip_count >= 1;
    if (plausible) {
        title->ssif_available = true;
        for (uint32_t clip = 0; clip < title->clip_count && title->ssif_available; clip++) {
            title->ssif_available = ssif_available(disc->bluray, info->clips[clip].clip_id);
        }
        title->clips_readable = title->ssif_available;
        for (uint32_t clip = 0; clip < title->clip_count && title->ssif_available && title->clips_readable; clip++) {
            title->clips_readable = clip_readable(disc->bluray, info->playlist, clip, &info->clips[clip]);
        }
    }
    bd_free_title_info(info);
    return true;
}

// MARK: - Demux

/// Reads Exp-Golomb codes from the start of a NAL unit's payload, skipping emulation-prevention
/// bytes. Reading past the end yields zero bits.
typedef struct {
    const uint8_t *data;
    size_t length;
    size_t byte;
    int bit;
    int zeros;
} BitReader;

static int read_bit(BitReader *reader) {
    if (reader->bit == 0) {
        if (reader->byte >= reader->length) {
            return 0;
        }
        if (reader->zeros >= 2 && reader->data[reader->byte] == 0x03) {
            reader->byte++;
            reader->zeros = 0;
            if (reader->byte >= reader->length) {
                return 0;
            }
        }
        reader->zeros = reader->data[reader->byte] == 0 ? reader->zeros + 1 : 0;
    }
    int value = (reader->data[reader->byte] >> (7 - reader->bit)) & 1;
    if (++reader->bit == 8) {
        reader->bit = 0;
        reader->byte++;
    }
    return value;
}

static uint32_t read_unsigned_golomb(BitReader *reader) {
    int leading = 0;
    while (leading < 32 && read_bit(reader) == 0) {
        leading++;
    }
    uint32_t value = 0;
    for (int index = 0; index < leading; index++) {
        value = (value << 1) | (uint32_t)read_bit(reader);
    }
    return (uint32_t)((UINT64_C(1) << leading) - 1 + value);
}

/// Whether decoding can start at this base-view access unit: its picture is an IDR, or an I
/// picture, as Blu-ray entry points often are (with the leading pictures after it dropped).
static bool is_random_access(const uint8_t *data, size_t length) {
    for (size_t index = 0; index + 3 < length; index++) {
        if (data[index] != 0 || data[index + 1] != 0 || data[index + 2] != 1) {
            continue;
        }
        uint8_t type = data[index + 3] & 0x1f;
        if (type == 5) {
            return true;
        }
        if (type == 1) {
            BitReader reader = {.data = data + index + 4, .length = length - index - 4};
            (void)read_unsigned_golomb(&reader); // first_mb_in_slice
            uint32_t slice_type = read_unsigned_golomb(&reader) % 5;
            return slice_type == 2 || slice_type == 4;
        }
        index += 3;
    }
    return false;
}

static void free_queue(PesQueue *queue) {
    PesItem *item = queue->head;
    while (item != NULL) {
        PesItem *next = item->next;
        free(item->data);
        free(item);
        item = next;
    }
    memset(queue, 0, sizeof(*queue));
}

static void free_demux(Demux *demux) {
    free(demux->base_builder.data);
    free(demux->dependent_builder.data);
    for (size_t index = 0; index < demux->selected_count; index++) {
        free(demux->selected[index].builder.data);
    }
    free_queue(&demux->base_pending);
    free_queue(&demux->dependent_pending);
}

static bd3d_result append_builder(PesBuilder *builder, const uint8_t *data, size_t size) {
    if (builder->length + size > MAX_PES_BYTES) {
        return BD3D_PENDING_LIMIT;
    }
    if (builder->length + size > builder->capacity) {
        size_t capacity = builder->capacity == 0 ? 65536 : builder->capacity;
        while (capacity < builder->length + size) {
            capacity *= 2;
        }
        uint8_t *resized = realloc(builder->data, capacity);
        if (resized == NULL) {
            return BD3D_OUT_OF_MEMORY;
        }
        builder->data = resized;
        builder->capacity = capacity;
    }
    memcpy(builder->data + builder->length, data, size);
    builder->length += size;
    return BD3D_OK;
}

static uint64_t parse_timestamp(const uint8_t *data) {
    return ((uint64_t)((data[0] >> 1) & 0x07) << 30) | ((uint64_t)data[1] << 22) |
           ((uint64_t)(data[2] >> 1) << 15) | ((uint64_t)data[3] << 7) | (uint64_t)(data[4] >> 1);
}

/// Writes a 33-bit timestamp over one in a PES header, keeping its prefix and marker bits.
static void write_timestamp(uint8_t *data, uint64_t timestamp) {
    data[0] = (uint8_t)((data[0] & 0xf1) | (((timestamp >> 30) & 0x07) << 1));
    data[1] = (uint8_t)(timestamp >> 22);
    data[2] = (uint8_t)((((timestamp >> 15) & 0x7f) << 1) | 1);
    data[3] = (uint8_t)(timestamp >> 7);
    data[4] = (uint8_t)(((timestamp & 0x7f) << 1) | 1);
}

/// A timestamp's signed distance from `origin` on the 33-bit clock.
static int64_t timestamp_distance(uint64_t timestamp, uint64_t origin) {
    int64_t distance = (int64_t)((timestamp - origin) & TIMESTAMP_MASK);
    return distance >= (INT64_C(1) << 32) ? distance - (INT64_C(1) << 33) : distance;
}

static uint64_t onto_playlist(const Demux *demux, uint64_t timestamp) {
    if (demux->segment == NULL) {
        return timestamp;
    }
    return (timestamp - demux->segment->in_time + demux->segment->playlist_time) & TIMESTAMP_MASK;
}

static PesItem *take_matching(PesQueue *queue, uint64_t dts) {
    for (PesItem **cursor = &queue->head; *cursor != NULL; cursor = &(*cursor)->next) {
        if ((*cursor)->dts == dts) {
            PesItem *item = *cursor;
            *cursor = item->next;
            item->next = NULL;
            queue->count--;
            queue->bytes -= item->capacity;
            return item;
        }
    }
    return NULL;
}

static bd3d_result append_queue(Demux *demux, PesQueue *queue, PesItem *item) {
    size_t pending = demux->base_pending.bytes + demux->dependent_pending.bytes + item->capacity;
    if (pending > MAX_PENDING_BYTES) {
        return BD3D_PENDING_LIMIT;
    }
    PesItem **cursor = &queue->head;
    while (*cursor != NULL) {
        cursor = &(*cursor)->next;
    }
    *cursor = item;
    queue->count++;
    queue->bytes += item->capacity;
    if (pending > demux->stats.maximum_pending_bytes) {
        demux->stats.maximum_pending_bytes = pending;
    }
    return BD3D_OK;
}

static bd3d_result emit(Demux *demux, const bd3d_packet *packet) {
    return demux->emit(demux->emit_context, packet) != 0 ? BD3D_STOPPED : BD3D_OK;
}

/// Whether `dts` comes before `previous` on the 33-bit 90 kHz clock. A forward step of less than
/// half the range is forward even when it wraps past zero; an equal value is not backward.
static bool dts_before(uint64_t dts, uint64_t previous) {
    return ((dts - previous) & TIMESTAMP_MASK) >= (UINT64_C(1) << 32);
}

/// Drops views read before the start whose pair can no longer arrive: both views of a frame
/// decoding before `dts`.
static void drop_pending_before(PesQueue *queue, uint64_t dts) {
    for (PesItem **cursor = &queue->head; *cursor != NULL;) {
        PesItem *item = *cursor;
        if (dts_before(item->dts, dts)) {
            *cursor = item->next;
            queue->count--;
            queue->bytes -= item->capacity;
            free(item->data);
            free(item);
        } else {
            cursor = &item->next;
        }
    }
}

static bd3d_result emit_pair(Demux *demux, PesItem *base, PesItem *dependent) {
    bool random_access = is_random_access(base->data, base->length);
    const bd3d_segment *segment = demux->segment;
    if (segment != NULL) {
        int64_t offset = timestamp_distance(base->pts, segment->in_time);
        if (!demux->started) {
            if (!random_access || offset < segment->start_offset) {
                return BD3D_OK;
            }
            demux->started = true;
            demux->start_pts = base->pts;
            drop_pending_before(&demux->base_pending, base->dts);
            drop_pending_before(&demux->dependent_pending, base->dts);
        } else if (timestamp_distance(base->pts, demux->start_pts) < 0) {
            return BD3D_OK;
        }
        if (segment->trim_end && random_access && offset >= (int64_t)segment->duration) {
            return SEGMENT_ENDED;
        }
    }
    if (demux->drop_unmatched) {
        drop_pending_before(&demux->base_pending, base->dts);
        drop_pending_before(&demux->dependent_pending, base->dts);
    }
    if (demux->stats.video_pairs > 0 && dts_before(base->dts, demux->last_dts)) {
        return BD3D_NON_MONOTONIC;
    }
    bd3d_packet packet = {
        .kind = BD3D_PACKET_VIDEO,
        .pid = BD3D_BASE_VIEW_PID,
        .random_access = random_access,
        .pts = onto_playlist(demux, base->pts),
        .dts = onto_playlist(demux, base->dts),
        .data = base->data,
        .length = base->length,
        .dependent = dependent->data,
        .dependent_length = dependent->length,
    };
    demux->last_dts = base->dts;
    demux->stats.video_pairs++;
    return emit(demux, &packet);
}

static SelectedStream *selected_stream(Demux *demux, uint16_t pid) {
    for (size_t index = 0; index < demux->selected_count; index++) {
        if (demux->selected[index].pid == pid) {
            return &demux->selected[index];
        }
    }
    return NULL;
}

static bd3d_result complete_view(Demux *demux, uint16_t pid, PesBuilder *builder) {
    PesItem *item = calloc(1, sizeof(*item));
    if (item == NULL) {
        return BD3D_OUT_OF_MEMORY;
    }
    item->data = builder->data;
    item->length = builder->length;
    item->capacity = builder->capacity;
    item->pts = builder->pts;
    item->dts = builder->dts;
    memset(builder, 0, sizeof(*builder));

    bool is_base = pid == BD3D_BASE_VIEW_PID;
    PesQueue *own = is_base ? &demux->base_pending : &demux->dependent_pending;
    PesQueue *opposite = is_base ? &demux->dependent_pending : &demux->base_pending;
    PesItem *match = take_matching(opposite, item->dts);
    if (match == NULL) {
        bd3d_result result = append_queue(demux, own, item);
        if (result != BD3D_OK) {
            free(item->data);
            free(item);
        }
        return result;
    }
    PesItem *base = is_base ? item : match;
    PesItem *dependent = is_base ? match : item;
    bd3d_result result = emit_pair(demux, base, dependent);
    free(base->data);
    free(base);
    free(dependent->data);
    free(dependent);
    return result;
}

/// Applies the segment to an audio or subtitle PES packet: whether it falls inside the segment,
/// with its timestamps moved onto the playlist timeline.
static bool place_selected_packet(Demux *demux, SelectedStream *stream, uint8_t *data, size_t length) {
    const bd3d_segment *segment = demux->segment;
    if (segment == NULL) {
        return true;
    }
    uint8_t flags = length >= 9 ? (data[7] >> 6) & 0x03 : 0;
    size_t header_length = length >= 9 ? data[8] : 0;
    if ((flags & 0x02) == 0 || header_length < (flags == 0x03 ? 10u : 5u) || length < 9 + header_length) {
        return stream->inside;
    }
    int64_t offset = timestamp_distance(parse_timestamp(data + 9), segment->in_time);
    stream->inside = offset >= 0 && offset < (int64_t)segment->duration;
    write_timestamp(data + 9, onto_playlist(demux, parse_timestamp(data + 9)));
    if (flags == 0x03) {
        write_timestamp(data + 14, onto_playlist(demux, parse_timestamp(data + 14)));
    }
    return stream->inside;
}

static bd3d_result complete_pes(Demux *demux, uint16_t pid, PesBuilder *builder) {
    if (!builder->active || builder->length == 0) {
        builder->active = false;
        builder->length = 0;
        return BD3D_OK;
    }
    SelectedStream *stream = selected_stream(demux, pid);
    if (stream == NULL) {
        return complete_view(demux, pid, builder);
    }
    bd3d_packet packet = {
        .kind = stream->kind,
        .pid = pid,
        .data = builder->data,
        .length = builder->length,
    };
    bool inside = place_selected_packet(demux, stream, builder->data, builder->length);
    builder->active = false;
    builder->length = 0;
    if (!inside) {
        return BD3D_OK;
    }
    if (stream->kind == BD3D_PACKET_AUDIO) {
        demux->stats.audio_packets++;
    } else {
        demux->stats.subtitle_packets++;
    }
    return emit(demux, &packet);
}

static bd3d_result process_packet(Demux *demux, const uint8_t *packet) {
    demux->stats.transport_packets++;
    if (packet[4] != 0x47) {
        return BD3D_INVALID_PACKET;
    }
    uint16_t pid = (uint16_t)(((packet[5] & 0x1f) << 8) | packet[6]);
    PesBuilder *builder;
    SelectedStream *stream = NULL;
    if (pid == BD3D_BASE_VIEW_PID) {
        builder = &demux->base_builder;
    } else if (pid == BD3D_DEPENDENT_VIEW_PID) {
        builder = &demux->dependent_builder;
    } else if ((stream = selected_stream(demux, pid)) != NULL) {
        builder = &stream->builder;
    } else {
        return BD3D_OK;
    }

    uint8_t adaptation = (packet[7] >> 4) & 0x03;
    if (adaptation == 0 || adaptation == 2) {
        return BD3D_OK;
    }
    size_t offset = 8;
    if (adaptation == 3) {
        offset += 1u + packet[8];
    }
    if (offset > M2TS_PACKET_SIZE) {
        return BD3D_INVALID_PACKET;
    }
    if (offset == M2TS_PACKET_SIZE) {
        return BD3D_OK;
    }

    if ((packet[5] & 0x40) != 0) {
        bd3d_result result = complete_pes(demux, pid, builder);
        if (result != BD3D_OK) {
            return result;
        }
        if (M2TS_PACKET_SIZE - offset < 9 || packet[offset] != 0x00 || packet[offset + 1] != 0x00 ||
            packet[offset + 2] != 0x01) {
            return BD3D_INVALID_PES;
        }
        if (stream != NULL) {
            // Audio and subtitle PES packets are delivered whole, header included.
            builder->active = true;
            return append_builder(builder, packet + offset, M2TS_PACKET_SIZE - offset);
        }
        uint8_t timestamp_flags = (packet[offset + 7] >> 6) & 0x03;
        uint8_t header_data_length = packet[offset + 8];
        size_t timestamp_bytes = timestamp_flags == 0x02 ? 5 : timestamp_flags == 0x03 ? 10 : 0;
        size_t header_size = 9u + header_data_length;
        if (timestamp_bytes == 0 || header_data_length < timestamp_bytes ||
            header_size > M2TS_PACKET_SIZE - offset) {
            return BD3D_INVALID_PES;
        }
        builder->pts = parse_timestamp(packet + offset + 9);
        builder->dts = timestamp_flags == 0x03 ? parse_timestamp(packet + offset + 14) : builder->pts;
        builder->active = true;
        offset += header_size;
    }
    if (!builder->active || offset >= M2TS_PACKET_SIZE) {
        return BD3D_OK;
    }
    return append_builder(builder, packet + offset, M2TS_PACKET_SIZE - offset);
}

static bd3d_result select_pids(
    Demux *demux, const uint16_t *pids, size_t count, bd3d_packet_kind kind
) {
    for (size_t index = 0; index < count; index++) {
        if (demux->selected_count == BD3D_MAX_SELECTED_PIDS) {
            return BD3D_TOO_MANY_PIDS;
        }
        uint16_t pid = pids[index];
        if (pid == BD3D_BASE_VIEW_PID || pid == BD3D_DEPENDENT_VIEW_PID || selected_stream(demux, pid) != NULL) {
            continue;
        }
        demux->selected[demux->selected_count++] = (SelectedStream){.pid = pid, .kind = kind, .inside = true};
    }
    return BD3D_OK;
}

static bd3d_result complete_builders(Demux *demux) {
    bd3d_result result = complete_pes(demux, BD3D_BASE_VIEW_PID, &demux->base_builder);
    if (result == BD3D_OK) {
        result = complete_pes(demux, BD3D_DEPENDENT_VIEW_PID, &demux->dependent_builder);
    }
    for (size_t index = 0; result == BD3D_OK && index < demux->selected_count; index++) {
        result = complete_pes(demux, demux->selected[index].pid, &demux->selected[index].builder);
    }
    return result;
}

static bd3d_result finish(Demux *demux) {
    bd3d_result result = complete_builders(demux);
    if (result == SEGMENT_ENDED) {
        return BD3D_OK;
    }
    if (result == BD3D_OK && !demux->drop_unmatched &&
        (demux->base_pending.count != 0 || demux->dependent_pending.count != 0)) {
        result = BD3D_UNMATCHED_PES;
    }
    if (result == BD3D_OK && demux->stats.video_pairs == 0) {
        result = BD3D_NO_MVC;
    }
    return result;
}

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
    bd3d_packet_fn emit_function,
    void *emit_context,
    bd3d_stats *stats
) {
    Demux demux = {
        .emit = emit_function, .emit_context = emit_context, .segment = segment, .started = segment == NULL,
    };
    bd3d_result result = select_pids(&demux, audio_pids, audio_count, BD3D_PACKET_AUDIO);
    if (result == BD3D_OK) {
        result = select_pids(&demux, subtitle_pids, subtitle_count, BD3D_PACKET_SUBTITLE);
    }
    uint8_t *buffer = result == BD3D_OK ? malloc(READ_CHUNK_SIZE) : NULL;
    if (result == BD3D_OK && buffer == NULL) {
        result = BD3D_OUT_OF_MEMORY;
    }
    // Reads may end mid-packet; the remainder carries over to the next read.
    size_t carried = 0;
    bool resyncing = false;
    size_t unsynced = 0;
    while (result == BD3D_OK) {
        if (monitor != NULL) {
            monitor(monitor_context, BD3D_READ_EVENT_STARTED, 0);
        }
        int64_t count;
        for (;;) {
            errno = 0;
            count = read(read_context, buffer + carried, (int64_t)(READ_CHUNK_SIZE - carried));
            if (count >= 0 || monitor == NULL || monitor(monitor_context, BD3D_READ_EVENT_FAILED, errno) == 0) {
                break;
            }
        }
        if (monitor != NULL) {
            monitor(monitor_context, BD3D_READ_EVENT_FINISHED, 0);
        }
        if (count < 0) {
            result = BD3D_READ_FAILED;
            break;
        }
        if (count == 0) {
            result = carried == 0 ? finish(&demux) : BD3D_INVALID_PACKET;
            break;
        }
        demux.stats.bytes += (uint64_t)count;
        size_t available = carried + (size_t)count;
        size_t offset = 0;
        while (result == BD3D_OK && offset + M2TS_PACKET_SIZE <= available) {
            if (!resyncing && buffer[offset + 4] == 0x47) {
                result = process_packet(&demux, buffer + offset);
                if (result == BD3D_INVALID_PES) {
                    // A packet with its sync byte but a damaged PES header: drop it, and with it
                    // the PES packet it starts.
                    result = BD3D_OK;
                    demux.drop_unmatched = true;
                    demux.stats.skipped_bytes += M2TS_PACKET_SIZE;
                }
                offset += M2TS_PACKET_SIZE;
                continue;
            }
            // Sync is lost, as across a damaged or unrecorded stretch of a disc image: deliver the
            // PES packets read so far, which may be cut short, and resume at the next two packets
            // in a row. A decoder conceals a cut picture or skips a cut audio frame.
            if (!resyncing) {
                resyncing = true;
                unsynced = 0;
                result = complete_builders(&demux);
                if (result != BD3D_OK) {
                    break;
                }
            }
            if (offset + M2TS_PACKET_SIZE + 5 > available) {
                break;
            }
            if (buffer[offset + 4] == 0x47 && buffer[offset + M2TS_PACKET_SIZE + 4] == 0x47) {
                resyncing = false;
                demux.drop_unmatched = true;
                continue;
            }
            offset++;
            demux.stats.skipped_bytes++;
            if (++unsynced > MAX_UNSYNCED_BYTES) {
                result = BD3D_INVALID_PACKET;
            }
        }
        carried = available - offset;
        if (result == BD3D_OK && carried > 0) {
            memmove(buffer, buffer + offset, carried);
        }
    }
    if (result == SEGMENT_ENDED) {
        result = BD3D_OK;
    }
    free(buffer);
    if (stats != NULL) {
        *stats = demux.stats;
    }
    free_demux(&demux);
    return result;
}

bd3d_result bd3d_demux(
    bd3d_read_fn read,
    void *read_context,
    bd3d_read_monitor_fn monitor,
    void *monitor_context,
    const uint16_t *audio_pids,
    size_t audio_count,
    const uint16_t *subtitle_pids,
    size_t subtitle_count,
    bd3d_packet_fn emit_function,
    void *emit_context,
    bd3d_stats *stats
) {
    return bd3d_demux_segment(
        read, read_context, NULL, monitor, monitor_context, audio_pids, audio_count, subtitle_pids, subtitle_count,
        emit_function, emit_context, stats
    );
}

// MARK: - Playlists

static int64_t read_bluray_file(void *context, uint8_t *buffer, int64_t size) {
    BD_FILE_H *file = context;
    int64_t position = file->tell(file);
    int64_t count = file->read(file, buffer, size);
    // Put the file back where the read started, so a monitor can try the same read again, and
    // keep the read's errno for the monitor.
    if (count < 0 && position >= 0) {
        int error = errno;
        file->seek(file, position, SEEK_SET);
        errno = error;
    }
    return count;
}

static void add_stats(bd3d_stats *total, const bd3d_stats *clip) {
    total->bytes += clip->bytes;
    total->transport_packets += clip->transport_packets;
    total->video_pairs += clip->video_pairs;
    total->skipped_bytes += clip->skipped_bytes;
    total->audio_packets += clip->audio_packets;
    total->subtitle_packets += clip->subtitle_packets;
    if (clip->maximum_pending_bytes > total->maximum_pending_bytes) {
        total->maximum_pending_bytes = clip->maximum_pending_bytes;
    }
}

/// Plans where to start reading a clip: from the start of its SSIF when the item starts where
/// the clip does and nothing is skipped, otherwise from the extent pair holding the last random
/// access unit at or before `clip_pts`.
static bd3d_result plan_clip_start(
    BLURAY *bluray, uint32_t playlist, uint32_t play_item, const BLURAY_CLIP_INFO *clip, uint64_t clip_pts,
    int64_t *byte_offset, bd3d_segment *segment
) {
    bd3d_clip_index base;
    if (!read_clip_index(bluray, clip->clip_id, 0, &base)) {
        return BD3D_INDEX_UNAVAILABLE;
    }
    segment->trim_end = clip->out_time < base.presentation_end;
    bool from_start = clip_pts <= base.presentation_start;
    bd3d_clip_index_free(&base);
    if (from_start) {
        *byte_offset = 0;
        segment->start_offset = INT64_MIN;
        return BD3D_OK;
    }
    bd3d_clip_index dependent;
    if (!read_stereo_indexes(bluray, playlist, play_item, clip->clip_id, &base, &dependent)) {
        return BD3D_INDEX_UNAVAILABLE;
    }
    const bd3d_entry_point *entry = bd3d_entry_point_at(&base, clip_pts);
    *byte_offset = entry == NULL ? -1 : bd3d_ssif_offset(&base, &dependent, entry->spn);
    if (entry != NULL) {
        segment->start_offset = timestamp_distance(entry->pts, clip->in_time);
    }
    bd3d_clip_index_free(&base);
    bd3d_clip_index_free(&dependent);
    return *byte_offset < 0 ? BD3D_INDEX_UNAVAILABLE : BD3D_OK;
}

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
    bd3d_packet_fn emit_function,
    void *emit_context,
    bd3d_stats *stats
) {
    BLURAY_TITLE_INFO *info = bd_get_playlist_info(disc->bluray, playlist, 0);
    if (info == NULL || info->clip_count == 0) {
        bd_free_title_info(info);
        return BD3D_TITLE_UNAVAILABLE;
    }
    uint32_t clip_count = title_clip_count(disc->bluray, playlist, info->clip_count);
    uint32_t first = 0;
    while (first + 1 < clip_count && info->clips[first + 1].start_time <= start_ticks) {
        first++;
    }
    bd3d_stats total = {0};
    bd3d_result result = BD3D_OK;
    for (uint32_t index = first; result == BD3D_OK && index < clip_count; index++) {
        const BLURAY_CLIP_INFO *clip = &info->clips[index];
        bd3d_segment segment = {
            .in_time = clip->in_time,
            .duration = clip->out_time - clip->in_time,
            .playlist_time = clip->start_time,
        };
        uint64_t clip_pts = clip->in_time;
        if (index == first && start_ticks > clip->start_time) {
            clip_pts += start_ticks - clip->start_time;
        }
        int64_t byte_offset = 0;
        result = plan_clip_start(disc->bluray, playlist, index, clip, clip_pts, &byte_offset, &segment);
        if (result != BD3D_OK) {
            break;
        }
        char path[64];
        ssif_path(path, sizeof(path), clip->clip_id);
        BD_FILE_H *file = bd_open_file_dec(disc->bluray, path);
        if (file == NULL) {
            result = BD3D_SSIF_UNAVAILABLE;
            break;
        }
        if (byte_offset > 0 && file->seek(file, byte_offset, SEEK_SET) != byte_offset) {
            file->close(file);
            result = BD3D_READ_FAILED;
            break;
        }
        bd3d_stats clip_stats = {0};
        result = bd3d_demux_segment(
            read_bluray_file, file, &segment, monitor, monitor_context, audio_pids, audio_count, subtitle_pids,
            subtitle_count, emit_function, emit_context, &clip_stats
        );
        file->close(file);
        add_stats(&total, &clip_stats);
    }
    bd_free_title_info(info);
    if (stats != NULL) {
        *stats = total;
    }
    return result;
}

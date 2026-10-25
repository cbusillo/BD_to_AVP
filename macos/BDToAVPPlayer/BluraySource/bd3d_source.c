#include "bd3d_source.h"

#include <libbluray/bluray.h>
#include <libbluray/clpi_data.h>
#include <libbluray/filesystem.h>
#include <libbluray/log_control.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define M2TS_PACKET_SIZE 192
#define READ_CHUNK_SIZE (6144 * 128)
#define MAX_PES_BYTES (16 * 1024 * 1024)
#define MAX_PENDING_BYTES (256 * 1024 * 1024)
#define TITLES_MINIMUM_SECONDS 60
#define TIMESTAMP_MASK ((UINT64_C(1) << 33) - 1)

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
} SelectedStream;

typedef struct {
    PesBuilder base_builder;
    PesBuilder dependent_builder;
    PesQueue base_pending;
    PesQueue dependent_pending;
    SelectedStream selected[BD3D_MAX_SELECTED_PIDS];
    size_t selected_count;
    uint64_t last_dts;
    bd3d_stats stats;
    bd3d_packet_fn emit;
    void *emit_context;
} Demux;

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

/// Whether the playlist plays its only clip from start to end, as the SSIF demuxer needs.
static bool covers_complete_clip(BLURAY *bluray, uint32_t playlist, const BLURAY_CLIP_INFO *clip) {
    if (!bd_select_playlist(bluray, playlist)) {
        return false;
    }
    CLPI_CL *clpi = bd_get_clpi(bluray, 0);
    if (clpi == NULL) {
        return false;
    }
    bool complete = false;
    if (clpi->sequence.num_atc_seq == 1 && clpi->sequence.atc_seq != NULL &&
        clpi->sequence.atc_seq[0].num_stc_seq == 1 && clpi->sequence.atc_seq[0].stc_seq != NULL) {
        CLPI_STC_SEQ *sequence = &clpi->sequence.atc_seq[0].stc_seq[0];
        complete = clip->pkt_count == clpi->clip.num_source_packets &&
                   clip->in_time == (uint64_t)sequence->presentation_start_time * 2 &&
                   clip->out_time == (uint64_t)sequence->presentation_end_time * 2;
    }
    bd_free_clpi(clpi);
    return complete;
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
    title->duration_ticks = info->duration;
    title->angle_count = info->angle_count;
    title->clip_count = info->clip_count;
    title->main_feature = info->playlist == disc->main_playlist;
    title->base_view_right = info->mvc_base_view_r_flag;
    if (info->clip_count >= 1) {
        const BLURAY_CLIP_INFO *clip = &info->clips[0];
        snprintf(title->clip_id, sizeof(title->clip_id), "%s", clip->clip_id);
        title->audio_count = copy_streams(title->audio, clip->audio_streams, clip->audio_stream_count);
        title->subtitle_count = copy_streams(title->subtitles, clip->pg_streams, clip->pg_stream_count);
    }
    // The SSIF and complete-clip checks run only for titles the structure allows.
    bool plausible = !disc->info.aacs_detected && !disc->info.bdplus_detected && disc->info.content_3d &&
                     info->angle_count == 1 && info->clip_count == 1;
    if (plausible) {
        title->ssif_available = ssif_available(disc->bluray, title->clip_id);
        title->complete_clip = covers_complete_clip(disc->bluray, info->playlist, &info->clips[0]);
    }
    bd_free_title_info(info);
    return true;
}

// MARK: - Demux

static bool contains_idr(const uint8_t *data, size_t length) {
    for (size_t index = 0; index + 3 < length; index++) {
        if (data[index] == 0 && data[index + 1] == 0 && data[index + 2] == 1 && (data[index + 3] & 0x1f) == 5) {
            return true;
        }
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

static bd3d_result emit_pair(Demux *demux, PesItem *base, PesItem *dependent) {
    if (demux->stats.video_pairs > 0 && dts_before(base->dts, demux->last_dts)) {
        return BD3D_NON_MONOTONIC;
    }
    bd3d_packet packet = {
        .kind = BD3D_PACKET_VIDEO,
        .pid = BD3D_BASE_VIEW_PID,
        .random_access = contains_idr(base->data, base->length),
        .pts = base->pts,
        .dts = base->dts,
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
    builder->active = false;
    builder->length = 0;
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
        demux->selected[demux->selected_count++] = (SelectedStream){.pid = pid, .kind = kind};
    }
    return BD3D_OK;
}

static bd3d_result finish(Demux *demux) {
    bd3d_result result = complete_pes(demux, BD3D_BASE_VIEW_PID, &demux->base_builder);
    if (result == BD3D_OK) {
        result = complete_pes(demux, BD3D_DEPENDENT_VIEW_PID, &demux->dependent_builder);
    }
    for (size_t index = 0; result == BD3D_OK && index < demux->selected_count; index++) {
        result = complete_pes(demux, demux->selected[index].pid, &demux->selected[index].builder);
    }
    if (result == BD3D_OK && (demux->base_pending.count != 0 || demux->dependent_pending.count != 0)) {
        result = BD3D_UNMATCHED_PES;
    }
    if (result == BD3D_OK && demux->stats.video_pairs == 0) {
        result = BD3D_NO_MVC;
    }
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
    Demux demux = {.emit = emit_function, .emit_context = emit_context};
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
    while (result == BD3D_OK) {
        if (monitor != NULL) {
            monitor(monitor_context, BD3D_READ_EVENT_STARTED);
        }
        int64_t count;
        do {
            count = read(read_context, buffer + carried, (int64_t)(READ_CHUNK_SIZE - carried));
        } while (count < 0 && monitor != NULL && monitor(monitor_context, BD3D_READ_EVENT_FAILED) != 0);
        if (monitor != NULL) {
            monitor(monitor_context, BD3D_READ_EVENT_FINISHED);
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
        for (; result == BD3D_OK && offset + M2TS_PACKET_SIZE <= available; offset += M2TS_PACKET_SIZE) {
            result = process_packet(&demux, buffer + offset);
        }
        carried = available - offset;
        if (result == BD3D_OK && carried > 0) {
            memmove(buffer, buffer + offset, carried);
        }
    }
    free(buffer);
    if (stats != NULL) {
        *stats = demux.stats;
    }
    free_demux(&demux);
    return result;
}

static int64_t read_bluray_file(void *context, uint8_t *buffer, int64_t size) {
    BD_FILE_H *file = context;
    int64_t position = file->tell(file);
    int64_t count = file->read(file, buffer, size);
    // Put the file back where the read started, so a monitor can try the same read again.
    if (count < 0 && position >= 0) {
        file->seek(file, position, SEEK_SET);
    }
    return count;
}

bd3d_result bd3d_disc_stream(
    bd3d_disc *disc,
    const char *clip_id,
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
    char path[64];
    ssif_path(path, sizeof(path), clip_id);
    BD_FILE_H *file = bd_open_file_dec(disc->bluray, path);
    if (file == NULL) {
        return BD3D_SSIF_UNAVAILABLE;
    }
    bd3d_result result = bd3d_demux(
        read_bluray_file, file, monitor, monitor_context, audio_pids, audio_count, subtitle_pids, subtitle_count, emit_function, emit_context,
        stats
    );
    file->close(file);
    return result;
}

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <libbluray/bluray.h>
#include <libbluray/clpi_data.h>
#include <libbluray/filesystem.h>
#include <libbluray/log_control.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define CONTRACT_VERSION 1
#define M2TS_PACKET_SIZE 192
#define READ_CHUNK_SIZE (6144 * 32)
#define MAX_PES_BYTES (16 * 1024 * 1024)
#define MAX_PENDING_BYTES (256 * 1024 * 1024)

typedef int64_t (*ReadFunction)(void *context, uint8_t *buffer, int64_t size);
typedef int (*WriteFunction)(void *context, const uint8_t *buffer, size_t size);

typedef enum {
    DEMUX_OK = 0,
    DEMUX_DONE = 1,
    DEMUX_READ_FAILED = -1,
    DEMUX_WRITE_FAILED = -2,
    DEMUX_INVALID_PACKET = -3,
    DEMUX_INVALID_PES = -4,
    DEMUX_OUT_OF_MEMORY = -5,
    DEMUX_PENDING_LIMIT = -6,
    DEMUX_NON_MONOTONIC = -7,
    DEMUX_UNMATCHED_PES = -8,
    DEMUX_NO_MVC_PES = -9,
    DEMUX_INSUFFICIENT_PAIRS = -10,
} DemuxResult;

typedef struct {
    uint8_t *data;
    size_t length;
    size_t capacity;
    uint64_t dts;
    bool active;
} PesBuilder;

typedef struct PesItem {
    uint8_t *data;
    size_t length;
    size_t capacity;
    uint64_t dts;
    struct PesItem *next;
} PesItem;

typedef struct {
    PesItem *head;
    size_t count;
    size_t bytes;
} PesQueue;

typedef struct {
    uint64_t packets;
    uint64_t pairs;
    uint64_t last_dts;
    size_t maximum_pending_bytes;
} DemuxStats;

typedef struct {
    PesBuilder base_builder;
    PesBuilder dependent_builder;
    PesQueue base_pending;
    PesQueue dependent_pending;
    DemuxStats stats;
    uint64_t maximum_pairs;
    WriteFunction write_function;
    void *write_context;
} DemuxContext;

typedef struct {
    BD_FILE_H *file;
} BlurayFileContext;

typedef struct {
    FILE *file;
} StandardFileContext;

static void write_json_string(FILE *stream, const char *value) {
    fputc('"', stream);
    for (const unsigned char *cursor = (const unsigned char *)value; *cursor != '\0'; cursor++) {
        switch (*cursor) {
            case '"':
                fputs("\\\"", stream);
                break;
            case '\\':
                fputs("\\\\", stream);
                break;
            case '\b':
                fputs("\\b", stream);
                break;
            case '\f':
                fputs("\\f", stream);
                break;
            case '\n':
                fputs("\\n", stream);
                break;
            case '\r':
                fputs("\\r", stream);
                break;
            case '\t':
                fputs("\\t", stream);
                break;
            default:
                if (*cursor < 0x20) {
                    fprintf(stream, "\\u%04x", *cursor);
                } else {
                    fputc(*cursor, stream);
                }
        }
    }
    fputc('"', stream);
}

static int print_error(const char *code, const char *message) {
    fputs("{\"schema_version\":1,\"type\":\"error\",\"code\":", stderr);
    write_json_string(stderr, code);
    fputs(",\"message\":", stderr);
    write_json_string(stderr, message);
    fputs("}\n", stderr);
    return 1;
}

static const char *demux_error_code(DemuxResult result) {
    switch (result) {
        case DEMUX_READ_FAILED:
            return "read_failed";
        case DEMUX_WRITE_FAILED:
            return "write_failed";
        case DEMUX_INVALID_PACKET:
            return "invalid_m2ts_packet";
        case DEMUX_INVALID_PES:
            return "invalid_pes";
        case DEMUX_OUT_OF_MEMORY:
            return "out_of_memory";
        case DEMUX_PENDING_LIMIT:
            return "pending_data_limit";
        case DEMUX_NON_MONOTONIC:
            return "non_monotonic_dts";
        case DEMUX_UNMATCHED_PES:
            return "unmatched_mvc_pes";
        case DEMUX_NO_MVC_PES:
            return "mvc_pids_unavailable";
        case DEMUX_INSUFFICIENT_PAIRS:
            return "insufficient_mvc_pairs";
        default:
            return "demux_failed";
    }
}

static void free_builder(PesBuilder *builder) {
    free(builder->data);
    memset(builder, 0, sizeof(*builder));
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

static void free_demux(DemuxContext *context) {
    free_builder(&context->base_builder);
    free_builder(&context->dependent_builder);
    free_queue(&context->base_pending);
    free_queue(&context->dependent_pending);
}

static DemuxResult append_builder(PesBuilder *builder, const uint8_t *data, size_t size) {
    if (builder->length + size > MAX_PES_BYTES) {
        return DEMUX_PENDING_LIMIT;
    }
    if (builder->length + size > builder->capacity) {
        size_t capacity = builder->capacity == 0 ? 65536 : builder->capacity;
        while (capacity < builder->length + size) {
            capacity *= 2;
        }
        uint8_t *resized = realloc(builder->data, capacity);
        if (resized == NULL) {
            return DEMUX_OUT_OF_MEMORY;
        }
        builder->data = resized;
        builder->capacity = capacity;
    }
    memcpy(builder->data + builder->length, data, size);
    builder->length += size;
    return DEMUX_OK;
}

static uint64_t parse_timestamp(const uint8_t *data) {
    return ((uint64_t)((data[0] >> 1) & 0x07) << 30) |
           ((uint64_t)data[1] << 22) |
           ((uint64_t)(data[2] >> 1) << 15) |
           ((uint64_t)data[3] << 7) |
           (uint64_t)(data[4] >> 1);
}

static PesItem *take_matching(PesQueue *queue, uint64_t dts) {
    PesItem **cursor = &queue->head;
    while (*cursor != NULL) {
        if ((*cursor)->dts == dts) {
            PesItem *item = *cursor;
            *cursor = item->next;
            item->next = NULL;
            queue->count--;
            queue->bytes -= item->capacity;
            return item;
        }
        cursor = &(*cursor)->next;
    }
    return NULL;
}

static DemuxResult append_queue(DemuxContext *context, PesQueue *queue, PesItem *item) {
    size_t pending_bytes = context->base_pending.bytes + context->dependent_pending.bytes + item->capacity;
    if (pending_bytes > MAX_PENDING_BYTES) {
        return DEMUX_PENDING_LIMIT;
    }
    PesItem **cursor = &queue->head;
    while (*cursor != NULL) {
        cursor = &(*cursor)->next;
    }
    *cursor = item;
    queue->count++;
    queue->bytes += item->capacity;
    if (pending_bytes > context->stats.maximum_pending_bytes) {
        context->stats.maximum_pending_bytes = pending_bytes;
    }
    return DEMUX_OK;
}

static DemuxResult emit_pair(DemuxContext *context, PesItem *base, PesItem *dependent) {
    if (context->stats.pairs > 0 && base->dts < context->stats.last_dts) {
        return DEMUX_NON_MONOTONIC;
    }
    int write_result = context->write_function(context->write_context, base->data, base->length);
    if (write_result > 0) {
        return DEMUX_DONE;
    }
    if (write_result < 0) {
        return DEMUX_WRITE_FAILED;
    }
    write_result = context->write_function(context->write_context, dependent->data, dependent->length);
    if (write_result > 0) {
        return DEMUX_DONE;
    }
    if (write_result < 0) {
        return DEMUX_WRITE_FAILED;
    }
    context->stats.last_dts = base->dts;
    context->stats.pairs++;
    if (context->maximum_pairs > 0 && context->stats.pairs >= context->maximum_pairs) {
        return DEMUX_DONE;
    }
    return DEMUX_OK;
}

static DemuxResult complete_pes(DemuxContext *context, uint16_t pid, PesBuilder *builder) {
    if (!builder->active || builder->length == 0) {
        builder->active = false;
        builder->length = 0;
        return DEMUX_OK;
    }

    PesItem *item = calloc(1, sizeof(*item));
    if (item == NULL) {
        return DEMUX_OUT_OF_MEMORY;
    }
    item->data = builder->data;
    item->length = builder->length;
    item->capacity = builder->capacity;
    item->dts = builder->dts;
    memset(builder, 0, sizeof(*builder));

    PesQueue *own_queue = pid == 0x1011 ? &context->base_pending : &context->dependent_pending;
    PesQueue *opposite_queue = pid == 0x1011 ? &context->dependent_pending : &context->base_pending;
    PesItem *match = take_matching(opposite_queue, item->dts);
    if (match == NULL) {
        DemuxResult queue_result = append_queue(context, own_queue, item);
        if (queue_result != DEMUX_OK) {
            free(item->data);
            free(item);
        }
        return queue_result;
    }

    PesItem *base = pid == 0x1011 ? item : match;
    PesItem *dependent = pid == 0x1012 ? item : match;
    DemuxResult result = emit_pair(context, base, dependent);
    free(base->data);
    free(base);
    free(dependent->data);
    free(dependent);
    return result;
}

static DemuxResult process_packet(DemuxContext *context, const uint8_t *packet) {
    context->stats.packets++;
    if (packet[4] != 0x47) {
        return DEMUX_INVALID_PACKET;
    }

    uint16_t pid = (uint16_t)(((packet[5] & 0x1f) << 8) | packet[6]);
    if (pid != 0x1011 && pid != 0x1012) {
        return DEMUX_OK;
    }

    uint8_t adaptation = (packet[7] >> 4) & 0x03;
    if (adaptation == 0 || adaptation == 2) {
        return DEMUX_OK;
    }

    size_t offset = 8;
    if (adaptation == 3) {
        offset += 1u + packet[8];
    }
    if (offset > M2TS_PACKET_SIZE) {
        return DEMUX_INVALID_PACKET;
    }
    if (offset == M2TS_PACKET_SIZE) {
        return DEMUX_OK;
    }

    PesBuilder *builder = pid == 0x1011 ? &context->base_builder : &context->dependent_builder;
    if ((packet[5] & 0x40) != 0) {
        DemuxResult complete_result = complete_pes(context, pid, builder);
        if (complete_result != DEMUX_OK) {
            return complete_result;
        }
        if (M2TS_PACKET_SIZE - offset < 9 || packet[offset] != 0x00 || packet[offset + 1] != 0x00 ||
            packet[offset + 2] != 0x01) {
            return DEMUX_INVALID_PES;
        }
        uint8_t flags = packet[offset + 7];
        uint8_t header_data_length = packet[offset + 8];
        uint8_t timestamp_flags = (flags >> 6) & 0x03;
        size_t required_timestamp_bytes = timestamp_flags == 0x02 ? 5 : timestamp_flags == 0x03 ? 10 : 0;
        size_t header_size = 9u + header_data_length;
        if (required_timestamp_bytes == 0 || header_data_length < required_timestamp_bytes ||
            header_size > M2TS_PACKET_SIZE - offset) {
            return DEMUX_INVALID_PES;
        }
        uint64_t presentation_timestamp = parse_timestamp(packet + offset + 9);
        builder->dts = timestamp_flags == 0x03 ? parse_timestamp(packet + offset + 14) : presentation_timestamp;
        builder->active = true;
        offset += header_size;
    }

    if (!builder->active || offset >= M2TS_PACKET_SIZE) {
        return DEMUX_OK;
    }
    return append_builder(builder, packet + offset, M2TS_PACKET_SIZE - offset);
}

static DemuxResult demux_stream(
    ReadFunction read_function,
    void *read_context,
    WriteFunction write_function,
    void *write_context,
    uint64_t maximum_pairs,
    DemuxStats *stats
) {
    DemuxContext context = {
        .maximum_pairs = maximum_pairs,
        .write_function = write_function,
        .write_context = write_context,
    };
    uint8_t buffer[READ_CHUNK_SIZE];
    DemuxResult result = DEMUX_OK;
    bool completed_early = false;

    while (result == DEMUX_OK) {
        int64_t count = read_function(read_context, buffer, sizeof(buffer));
        if (count < 0) {
            result = DEMUX_READ_FAILED;
            break;
        }
        if (count == 0) {
            break;
        }
        if (count % M2TS_PACKET_SIZE != 0) {
            result = DEMUX_INVALID_PACKET;
            break;
        }
        for (int64_t offset = 0; offset < count; offset += M2TS_PACKET_SIZE) {
            result = process_packet(&context, buffer + offset);
            if (result != DEMUX_OK) {
                break;
            }
        }
    }

    if (result == DEMUX_DONE) {
        completed_early = true;
        result = DEMUX_OK;
    }
    if (result == DEMUX_OK && !completed_early) {
        result = complete_pes(&context, 0x1011, &context.base_builder);
    }
    if (result == DEMUX_DONE) {
        completed_early = true;
        result = DEMUX_OK;
    }
    if (result == DEMUX_OK && !completed_early) {
        result = complete_pes(&context, 0x1012, &context.dependent_builder);
    }
    if (result == DEMUX_DONE) {
        completed_early = true;
        result = DEMUX_OK;
    }
    if (result == DEMUX_OK && !completed_early &&
        (context.base_pending.count != 0 || context.dependent_pending.count != 0)) {
        result = DEMUX_UNMATCHED_PES;
    }
    if (result == DEMUX_OK && !completed_early && context.stats.pairs == 0) {
        result = DEMUX_NO_MVC_PES;
    }
    if (result == DEMUX_OK && !completed_early && maximum_pairs > 0 && context.stats.pairs < maximum_pairs) {
        result = DEMUX_INSUFFICIENT_PAIRS;
    }

    *stats = context.stats;
    free_demux(&context);
    return result;
}

static int64_t read_bluray_file(void *context, uint8_t *buffer, int64_t size) {
    BlurayFileContext *file_context = context;
    return file_context->file->read(file_context->file, buffer, size);
}

static int64_t read_standard_file(void *context, uint8_t *buffer, int64_t size) {
    StandardFileContext *file_context = context;
    if (ferror(file_context->file)) {
        return -1;
    }
    if (feof(file_context->file)) {
        return 0;
    }
    size_t count = fread(buffer, 1, (size_t)size, file_context->file);
    if (count == 0 && ferror(file_context->file)) {
        return -1;
    }
    return (int64_t)count;
}

static int write_standard_file(void *context, const uint8_t *buffer, size_t size) {
    FILE *file = context;
    while (size > 0) {
        size_t written = fwrite(buffer, 1, size, file);
        if (written == 0) {
            return errno == EPIPE ? 1 : -1;
        }
        buffer += written;
        size -= written;
    }
    return 0;
}

static bool parse_unsigned(const char *value, uint64_t maximum, uint64_t *parsed) {
    if (value == NULL || *value == '\0' || *value == '-') {
        return false;
    }
    errno = 0;
    char *end = NULL;
    unsigned long long number = strtoull(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || number > maximum) {
        return false;
    }
    *parsed = (uint64_t)number;
    return true;
}

static bool validate_source_path(const char *path, const char **source_kind) {
    struct stat source_stat;
    if (stat(path, &source_stat) != 0) {
        return false;
    }
    if (S_ISREG(source_stat.st_mode)) {
        *source_kind = "disc_image";
        return true;
    }
    if (S_ISDIR(source_stat.st_mode)) {
        *source_kind = "blu_ray_folder";
        return true;
    }
    return false;
}

static BLURAY *open_bluray(const char *source_path) {
    fflush(stderr);
    int saved_stderr = dup(STDERR_FILENO);
    int null_output = open("/dev/null", O_WRONLY);
    if (saved_stderr >= 0 && null_output >= 0) {
        dup2(null_output, STDERR_FILENO);
    }
    BLURAY *bluray = bd_open(source_path, NULL);
    fflush(stderr);
    if (saved_stderr >= 0) {
        dup2(saved_stderr, STDERR_FILENO);
        close(saved_stderr);
    }
    if (null_output >= 0) {
        close(null_output);
    }
    return bluray;
}

static void print_language(const uint8_t language[4]) {
    if (language[0] == 0) {
        fputs("null", stdout);
        return;
    }
    char value[4] = {(char)language[0], (char)language[1], (char)language[2], '\0'};
    write_json_string(stdout, value);
}

static void print_streams(const BLURAY_STREAM_INFO *streams, uint8_t count) {
    fputc('[', stdout);
    for (uint8_t index = 0; index < count; index++) {
        if (index > 0) {
            fputc(',', stdout);
        }
        fprintf(
            stdout,
            "{\"pid\":%u,\"coding_type\":%u,\"format\":%u,\"rate\":%u,\"language\":",
            streams[index].pid,
            streams[index].coding_type,
            streams[index].format,
            streams[index].rate
        );
        print_language(streams[index].lang);
        fprintf(stdout, ",\"subpath_id\":%u}", streams[index].subpath_id);
    }
    fputc(']', stdout);
}

static int64_t ssif_size(BLURAY *bluray, const char *path) {
    BD_FILE_H *file = bd_open_file_dec(bluray, path);
    if (file == NULL) {
        return -1;
    }
    int64_t size = file->seek(file, 0, SEEK_END);
    file->close(file);
    return size;
}

static bool playlist_covers_complete_clip(
    BLURAY *bluray,
    uint32_t playlist,
    const BLURAY_CLIP_INFO *clip
) {
    if (!bd_select_playlist(bluray, playlist)) {
        return false;
    }
    CLPI_CL *clpi = bd_get_clpi(bluray, 0);
    if (clpi == NULL) {
        return false;
    }
    bool complete_clip = false;
    if (clpi->sequence.num_atc_seq == 1 && clpi->sequence.atc_seq != NULL &&
        clpi->sequence.atc_seq[0].num_stc_seq == 1 && clpi->sequence.atc_seq[0].stc_seq != NULL) {
        CLPI_STC_SEQ *sequence = &clpi->sequence.atc_seq[0].stc_seq[0];
        uint64_t presentation_start = (uint64_t)sequence->presentation_start_time * 2;
        uint64_t presentation_end = (uint64_t)sequence->presentation_end_time * 2;
        complete_clip = clip->pkt_count == clpi->clip.num_source_packets &&
                        clip->in_time == presentation_start &&
                        clip->out_time == presentation_end;
    }
    bd_free_clpi(clpi);
    return complete_clip;
}

static const char *unsupported_reason(
    bool aacs_detected,
    bool bdplus_detected,
    bool content_exists_3d,
    const BLURAY_TITLE_INFO *title_info,
    int64_t selected_ssif_size,
    bool complete_clip
) {
    if (aacs_detected || bdplus_detected) {
        return "encrypted_source_unsupported";
    }
    if (!content_exists_3d) {
        return "source_is_not_3d";
    }
    if (title_info->angle_count != 1) {
        return "multiple_angles_unsupported";
    }
    if (title_info->clip_count != 1) {
        return "multiple_clips_unsupported";
    }
    if (!complete_clip) {
        return "partial_clip_unsupported";
    }
    if (selected_ssif_size < 0) {
        return "ssif_unavailable";
    }
    return NULL;
}

static int inspect_source(const char *source_path, uint32_t playlist) {
    const char *source_kind = NULL;
    if (!validate_source_path(source_path, &source_kind)) {
        return print_error("invalid_source", "The source must be an existing ISO image or Blu-ray folder.");
    }

    bd_set_debug_mask(0);
    BLURAY *bluray = open_bluray(source_path);
    if (bluray == NULL) {
        return print_error("source_open_failed", "libbluray could not open the source.");
    }
    const BLURAY_DISC_INFO *disc_info = bd_get_disc_info(bluray);
    if (disc_info == NULL) {
        bd_close(bluray);
        return print_error("disc_info_unavailable", "libbluray did not return disc information.");
    }
    bool content_exists_3d = disc_info->content_exist_3D;
    bool aacs_detected = disc_info->aacs_detected;
    bool aacs_handled = disc_info->aacs_handled;
    bool bdplus_detected = disc_info->bdplus_detected;
    bool bdplus_handled = disc_info->bdplus_handled;

    uint32_t title_count = bd_get_titles(bluray, TITLES_ALL, 0);
    BLURAY_TITLE_INFO *title_info = bd_get_playlist_info(bluray, playlist, 0);
    if (title_info == NULL) {
        bd_close(bluray);
        return print_error("title_unavailable", "The requested playlist is not available.");
    }

    uint32_t main_playlist = 0;
    int main_title_index = bd_get_main_title(bluray);
    if (main_title_index >= 0) {
        BLURAY_TITLE_INFO *main_title = bd_get_title_info(bluray, (uint32_t)main_title_index, 0);
        if (main_title != NULL) {
            main_playlist = main_title->playlist;
            bd_free_title_info(main_title);
        }
    }

    int64_t *clip_ssif_sizes = calloc(title_info->clip_count, sizeof(*clip_ssif_sizes));
    if (clip_ssif_sizes == NULL && title_info->clip_count > 0) {
        bd_free_title_info(title_info);
        bd_close(bluray);
        return print_error("out_of_memory", "The SSIF inspection could not allocate clip metadata.");
    }
    for (uint32_t index = 0; index < title_info->clip_count; index++) {
        char ssif_path[64];
        snprintf(ssif_path, sizeof(ssif_path), "BDMV/STREAM/SSIF/%s.ssif", title_info->clips[index].clip_id);
        clip_ssif_sizes[index] = ssif_size(bluray, ssif_path);
    }
    int64_t selected_ssif_size = title_info->clip_count == 1 ? clip_ssif_sizes[0] : -1;
    bool complete_clip = title_info->clip_count == 1 &&
                         playlist_covers_complete_clip(bluray, playlist, &title_info->clips[0]);
    const char *reason = unsupported_reason(
        aacs_detected,
        bdplus_detected,
        content_exists_3d,
        title_info,
        selected_ssif_size,
        complete_clip
    );
    int libbluray_major = 0;
    int libbluray_minor = 0;
    int libbluray_micro = 0;
    bd_get_version(&libbluray_major, &libbluray_minor, &libbluray_micro);

    fprintf(
        stdout,
        "{\"schema_version\":%d,\"type\":\"source.inspect\",\"libbluray_version\":\"%d.%d.%d\","
        "\"source_kind\":\"%s\",\"title_count\":%u,\"content_3d\":%s,\"aacs_detected\":%s,"
        "\"aacs_handled\":%s,\"bdplus_detected\":%s,\"bdplus_handled\":%s,"
        "\"title\":{\"id\":\"ssif:playlist:%05u\",\"playlist\":%u,"
        "\"duration_ticks\":%" PRIu64 ",\"duration_seconds\":%.6f,\"angle_count\":%u,"
        "\"clip_count\":%u,\"main_feature\":%s,\"mvc_base_view\":\"%s\",\"eligible\":%s,"
        "\"complete_clip\":%s,\"mvc_pids\":{\"base\":4113,\"dependent\":4114},"
        "\"unsupported_reason\":",
        CONTRACT_VERSION,
        libbluray_major,
        libbluray_minor,
        libbluray_micro,
        source_kind,
        title_count,
        content_exists_3d ? "true" : "false",
        aacs_detected ? "true" : "false",
        aacs_handled ? "true" : "false",
        bdplus_detected ? "true" : "false",
        bdplus_handled ? "true" : "false",
        playlist,
        playlist,
        title_info->duration,
        (double)title_info->duration / 90000.0,
        title_info->angle_count,
        title_info->clip_count,
        playlist == main_playlist ? "true" : "false",
        title_info->mvc_base_view_r_flag ? "right" : "left",
        reason == NULL ? "true" : "false",
        complete_clip ? "true" : "false"
    );
    if (reason == NULL) {
        fputs("null", stdout);
    } else {
        write_json_string(stdout, reason);
    }
    fputs(",\"clips\":[", stdout);

    for (uint32_t index = 0; index < title_info->clip_count; index++) {
        BLURAY_CLIP_INFO *clip = &title_info->clips[index];
        if (index > 0) {
            fputc(',', stdout);
        }
        char ssif_path[64];
        snprintf(ssif_path, sizeof(ssif_path), "BDMV/STREAM/SSIF/%s.ssif", clip->clip_id);
        int64_t clip_ssif_size = clip_ssif_sizes[index];
        fprintf(
            stdout,
            "{\"id\":\"%s\",\"ssif_path\":\"%s\",\"ssif_size_bytes\":",
            clip->clip_id,
            ssif_path
        );
        if (clip_ssif_size < 0) {
            fputs("null", stdout);
        } else {
            fprintf(stdout, "%" PRId64, clip_ssif_size);
        }
        fprintf(
            stdout,
            ",\"packet_count\":%u,\"start_ticks\":%" PRIu64 ",\"in_ticks\":%" PRIu64
            ",\"out_ticks\":%" PRIu64 ",\"video_streams\":",
            clip->pkt_count,
            clip->start_time,
            clip->in_time,
            clip->out_time
        );
        print_streams(clip->video_streams, clip->video_stream_count);
        fputs(",\"secondary_video_streams\":", stdout);
        print_streams(clip->sec_video_streams, clip->sec_video_stream_count);
        fputs(",\"audio_streams\":", stdout);
        print_streams(clip->audio_streams, clip->audio_stream_count);
        fputs(",\"pg_streams\":", stdout);
        print_streams(clip->pg_streams, clip->pg_stream_count);
        fputc('}', stdout);
    }
    fputs("]}}\n", stdout);

    free(clip_ssif_sizes);
    bd_free_title_info(title_info);
    bd_close(bluray);
    return 0;
}

static int stream_mvc(const char *source_path, uint32_t playlist, uint64_t maximum_pairs) {
    const char *source_kind = NULL;
    if (!validate_source_path(source_path, &source_kind)) {
        return print_error("invalid_source", "The source must be an existing ISO image or Blu-ray folder.");
    }
    (void)source_kind;

    bd_set_debug_mask(0);
    BLURAY *bluray = open_bluray(source_path);
    if (bluray == NULL) {
        return print_error("source_open_failed", "libbluray could not open the source.");
    }
    const BLURAY_DISC_INFO *disc_info = bd_get_disc_info(bluray);
    if (disc_info == NULL) {
        bd_close(bluray);
        return print_error("disc_info_unavailable", "libbluray did not return disc information.");
    }
    bool content_exists_3d = disc_info->content_exist_3D;
    bool aacs_detected = disc_info->aacs_detected;
    bool bdplus_detected = disc_info->bdplus_detected;
    bd_get_titles(bluray, TITLES_ALL, 0);
    BLURAY_TITLE_INFO *title_info = bd_get_playlist_info(bluray, playlist, 0);
    if (title_info == NULL) {
        bd_close(bluray);
        return print_error("title_unavailable", "The requested playlist is not available.");
    }

    int64_t selected_ssif_size = -1;
    char ssif_path[64] = {0};
    if (title_info->clip_count == 1) {
        snprintf(ssif_path, sizeof(ssif_path), "BDMV/STREAM/SSIF/%s.ssif", title_info->clips[0].clip_id);
        selected_ssif_size = ssif_size(bluray, ssif_path);
    }
    bool complete_clip = title_info->clip_count == 1 &&
                         playlist_covers_complete_clip(bluray, playlist, &title_info->clips[0]);
    const char *reason = unsupported_reason(
        aacs_detected,
        bdplus_detected,
        content_exists_3d,
        title_info,
        selected_ssif_size,
        complete_clip
    );
    if (reason != NULL) {
        bd_free_title_info(title_info);
        bd_close(bluray);
        return print_error(reason, "The requested playlist is outside the bounded direct-SSIF prototype.");
    }

    bd_free_title_info(title_info);
    bd_close(bluray);
    bluray = open_bluray(source_path);
    if (bluray == NULL) {
        return print_error("source_open_failed", "libbluray could not reopen the source for SSIF streaming.");
    }
    BD_FILE_H *file = bd_open_file_dec(bluray, ssif_path);
    if (file == NULL) {
        bd_close(bluray);
        return print_error("ssif_unavailable", "The selected SSIF stream could not be opened.");
    }
    BlurayFileContext read_context = {.file = file};
    DemuxStats stats = {0};
    DemuxResult result = demux_stream(
        read_bluray_file,
        &read_context,
        write_standard_file,
        stdout,
        maximum_pairs,
        &stats
    );
    file->close(file);
    bd_close(bluray);

    if (result != DEMUX_OK) {
        return print_error(demux_error_code(result), "The MVC transport stream could not be reconstructed.");
    }
    fprintf(
        stderr,
        "{\"schema_version\":%d,\"type\":\"stream.complete\",\"playlist\":%u,"
        "\"packets\":%" PRIu64 ",\"pairs\":%" PRIu64 ",\"maximum_pending_bytes\":%zu}\n",
        CONTRACT_VERSION,
        playlist,
        stats.packets,
        stats.pairs,
        stats.maximum_pending_bytes
    );
    return 0;
}

static int demux_file(const char *input_path, uint64_t maximum_pairs) {
    FILE *input = fopen(input_path, "rb");
    if (input == NULL) {
        return print_error("input_open_failed", "The M2TS input file could not be opened.");
    }
    StandardFileContext read_context = {.file = input};
    DemuxStats stats = {0};
    DemuxResult result = demux_stream(
        read_standard_file,
        &read_context,
        write_standard_file,
        stdout,
        maximum_pairs,
        &stats
    );
    fclose(input);
    if (result != DEMUX_OK) {
        return print_error(demux_error_code(result), "The MVC transport stream could not be reconstructed.");
    }
    fprintf(
        stderr,
        "{\"schema_version\":%d,\"type\":\"stream.complete\",\"packets\":%" PRIu64
        ",\"pairs\":%" PRIu64 ",\"maximum_pending_bytes\":%zu}\n",
        CONTRACT_VERSION,
        stats.packets,
        stats.pairs,
        stats.maximum_pending_bytes
    );
    return 0;
}

static int print_usage(void) {
    fputs(
        "Usage:\n"
        "  ssif_probe inspect <source> <playlist>\n"
        "  ssif_probe stream-mvc <source> <playlist> [maximum-pairs]\n"
        "  ssif_probe demux-file <m2ts-file> [maximum-pairs]\n"
        "  ssif_probe --version\n",
        stderr
    );
    return 2;
}

int main(int argc, char **argv) {
    signal(SIGPIPE, SIG_IGN);
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        printf("ssif_probe contract %d\n", CONTRACT_VERSION);
        return 0;
    }
    if (argc == 4 && strcmp(argv[1], "inspect") == 0) {
        uint64_t playlist = 0;
        if (!parse_unsigned(argv[3], 99999, &playlist)) {
            return print_error("invalid_playlist", "The playlist must be an integer from 0 through 99999.");
        }
        return inspect_source(argv[2], (uint32_t)playlist);
    }
    if ((argc == 4 || argc == 5) && strcmp(argv[1], "stream-mvc") == 0) {
        uint64_t playlist = 0;
        uint64_t maximum_pairs = 0;
        if (!parse_unsigned(argv[3], 99999, &playlist) ||
            (argc == 5 && !parse_unsigned(argv[4], UINT64_MAX, &maximum_pairs))) {
            return print_error("invalid_arguments", "Playlist and maximum-pairs must be non-negative integers.");
        }
        return stream_mvc(argv[2], (uint32_t)playlist, maximum_pairs);
    }
    if ((argc == 3 || argc == 4) && strcmp(argv[1], "demux-file") == 0) {
        uint64_t maximum_pairs = 0;
        if (argc == 4 && !parse_unsigned(argv[3], UINT64_MAX, &maximum_pairs)) {
            return print_error("invalid_arguments", "maximum-pairs must be a non-negative integer.");
        }
        return demux_file(argv[2], maximum_pairs);
    }
    return print_usage();
}

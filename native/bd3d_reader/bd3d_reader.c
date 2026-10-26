// Reads an unencrypted Blu-ray 3D image or folder for the Mac converter, with the demuxer the
// headset uses (macos/BDToAVPPlayer/BluraySource/bd3d_source.c). It never decrypts: a source
// with an AACS or BDSVM folder is refused before libbluray opens it.
//
//   bd3d_reader titles <source>
//       Prints the disc and its titles of at least 60 seconds as JSON.
//   bd3d_reader stream <source> <playlist> [--sidecar <path>] [--audio <pid,...>]
//                      [--subtitles <pid,...>] [--maximum-pairs <count>]
//       Writes the playlist's MVC video to stdout as Annex B, each base-view access unit followed
//       by its dependent view. With --sidecar it also writes the selected audio and
//       presentation-graphics streams to an MPEG transport stream whose PMT carries the stream
//       languages; each DTS-HD stream is followed by its DTS core as its own stream. The
//       sidecar also carries the first base-view access unit, so a reader of the sidecar measures
//       the audio start against the video start, as it does in a MakeMKV MKV.
//   bd3d_reader excerpt <source> <playlist> <start-seconds> <seconds>
//       Writes the 2D (base view) transport stream of a playlist range to stdout.
//   bd3d_reader --version

#include "bd3d_source.h"

#include <errno.h>
#include <inttypes.h>
#include <libbluray/bluray.h>
#include <libbluray/log_control.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <udfread/udfread.h>
#include <unistd.h>

#define CONTRACT_VERSION 1
#define TS_PACKET_SIZE 188
#define TS_PAYLOAD_SIZE 184
#define PMT_PID 0x0100
#define MAX_SIDECAR_STREAMS (BD3D_MAX_SELECTED_PIDS * 2 + 1)
#define STREAM_TYPE_H264 0x1b
#define DTS_CORE_PID_OFFSET 0x0800
#define CODING_TYPE_DTS 0x82
#define CODING_TYPE_DTS_HD_HRA 0x85
#define CODING_TYPE_DTS_HD_MA 0x86
#define BD_TICKS_PER_SECOND 90000
#define EXCERPT_READ_SIZE (6144 * 32)

// MARK: - Output helpers

static void write_json_string(FILE *stream, const char *value) {
    fputc('"', stream);
    for (const unsigned char *cursor = (const unsigned char *)value; *cursor != '\0'; cursor++) {
        if (*cursor == '"' || *cursor == '\\') {
            fputc('\\', stream);
            fputc(*cursor, stream);
        } else if (*cursor < 0x20) {
            fprintf(stream, "\\u%04x", *cursor);
        } else {
            fputc(*cursor, stream);
        }
    }
    fputc('"', stream);
}

static int print_error(const char *code, const char *message) {
    fputs("{\"type\":\"error\",\"code\":", stderr);
    write_json_string(stderr, code);
    fputs(",\"message\":", stderr);
    write_json_string(stderr, message);
    fputs("}\n", stderr);
    return 1;
}

static bool write_all(int descriptor, const uint8_t *data, size_t length) {
    while (length > 0) {
        ssize_t written = write(descriptor, data, length);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return false;
        }
        data += written;
        length -= (size_t)written;
    }
    return true;
}

static const char *result_code(bd3d_result result) {
    switch (result) {
        case BD3D_READ_FAILED:
            return "read_failed";
        case BD3D_INVALID_PACKET:
            return "invalid_packet";
        case BD3D_INVALID_PES:
            return "invalid_pes";
        case BD3D_OUT_OF_MEMORY:
            return "out_of_memory";
        case BD3D_PENDING_LIMIT:
            return "pending_limit";
        case BD3D_NON_MONOTONIC:
            return "non_monotonic";
        case BD3D_UNMATCHED_PES:
            return "unmatched_pes";
        case BD3D_NO_MVC:
            return "no_mvc";
        case BD3D_TOO_MANY_PIDS:
            return "too_many_pids";
        case BD3D_SSIF_UNAVAILABLE:
            return "ssif_unavailable";
        case BD3D_TITLE_UNAVAILABLE:
            return "title_unavailable";
        case BD3D_INDEX_UNAVAILABLE:
            return "index_unavailable";
        default:
            return "demux_failed";
    }
}

// MARK: - Encryption check

typedef struct {
    bool readable;
    bool encrypted;
    char volume_id[128];
} SourceCheck;

static bool directory_exists(const char *root, const char *name) {
    char path[4096];
    struct stat status;
    snprintf(path, sizeof(path), "%s/%s", root, name);
    return stat(path, &status) == 0 && S_ISDIR(status.st_mode);
}

static bool image_directory_exists(udfread *image, const char *name) {
    UDFDIR *directory = udfread_opendir(image, name);
    if (directory == NULL) {
        return false;
    }
    udfread_closedir(directory);
    return true;
}

/// Looks for the AACS and BD+ folders without libbluray, so no decryption library ever loads.
static SourceCheck check_source(const char *path) {
    SourceCheck check = {0};
    struct stat status;
    if (stat(path, &status) != 0) {
        return check;
    }
    if (S_ISDIR(status.st_mode)) {
        check.readable = directory_exists(path, "BDMV");
        check.encrypted = directory_exists(path, "AACS") || directory_exists(path, "BDSVM");
        return check;
    }
    if (!S_ISREG(status.st_mode)) {
        return check;
    }
    udfread *image = udfread_init();
    if (image == NULL) {
        return check;
    }
    if (udfread_open(image, path) == 0) {
        check.readable = image_directory_exists(image, "/BDMV");
        check.encrypted = image_directory_exists(image, "/AACS") || image_directory_exists(image, "/BDSVM");
        const char *volume_id = udfread_get_volume_id(image);
        if (volume_id != NULL) {
            snprintf(check.volume_id, sizeof(check.volume_id), "%s", volume_id);
        }
    }
    udfread_close(image);
    return check;
}

static int refuse_unreadable(const SourceCheck *check) {
    if (!check->readable) {
        return print_error("not_bluray", "The source is not a readable Blu-ray image or folder.");
    }
    return print_error("encrypted", "The source has disc encryption and is read only through MakeMKV.");
}

// MARK: - Titles

static void print_language(FILE *out, const char *language) {
    for (int index = 0; index < 3; index++) {
        if (language[index] < 'a' || language[index] > 'z') {
            fputs("null", out);
            return;
        }
    }
    write_json_string(out, language);
}

static void print_stream(FILE *out, const bd3d_stream *stream) {
    fprintf(
        out,
        "{\"pid\":%u,\"coding_type\":%u,\"format\":%u,\"rate\":%u,\"language\":",
        stream->pid,
        stream->coding_type,
        stream->format,
        stream->rate
    );
    print_language(out, stream->language);
    fputc('}', out);
}

static void print_streams(FILE *out, const bd3d_stream *streams, uint8_t count) {
    fputc('[', out);
    for (uint8_t index = 0; index < count; index++) {
        if (index > 0) {
            fputc(',', out);
        }
        print_stream(out, &streams[index]);
    }
    fputc(']', out);
}

static int list_titles(const char *path) {
    SourceCheck check = check_source(path);
    if (!check.readable || check.encrypted) {
        return refuse_unreadable(&check);
    }
    bd3d_disc *disc = bd3d_disc_open(path);
    if (disc == NULL) {
        return print_error("open_failed", "libbluray could not open the source.");
    }
    bd3d_disc_info info = bd3d_disc_get_info(disc);
    FILE *out = stdout;
    fprintf(out, "{\"type\":\"bd3d.titles\",\"contract\":%d,\"volume_id\":", CONTRACT_VERSION);
    if (check.volume_id[0] != '\0') {
        write_json_string(out, check.volume_id);
    } else {
        fputs("null", out);
    }
    fprintf(out, ",\"content_3d\":%s,\"titles\":[", info.content_3d ? "true" : "false");
    bool first = true;
    for (uint32_t index = 0; index < info.title_count; index++) {
        bd3d_title title;
        if (!bd3d_disc_title(disc, index, &title)) {
            continue;
        }
        if (!first) {
            fputc(',', out);
        }
        first = false;
        bool readable = title.ssif_available && title.clips_readable;
        fprintf(
            out,
            "{\"playlist\":%u,\"duration_ticks\":%" PRIu64 ",\"main_feature\":%s,\"clip_id\":",
            title.playlist,
            title.duration_ticks,
            title.main_feature ? "true" : "false"
        );
        write_json_string(out, title.clip_id);
        fprintf(
            out,
            ",\"clip_count\":%u,\"angle_count\":%u,\"readable\":%s,\"base_view_right\":%s,\"video\":",
            title.clip_count,
            title.angle_count,
            readable ? "true" : "false",
            title.base_view_right ? "true" : "false"
        );
        if (title.video_count > 0) {
            print_stream(out, &title.video);
        } else {
            fputs("null", out);
        }
        fputs(",\"audio\":", out);
        print_streams(out, title.audio, title.audio_count);
        fputs(",\"subtitles\":", out);
        print_streams(out, title.subtitles, title.subtitle_count);
        fputc('}', out);
    }
    fputs("]}\n", out);
    bd3d_disc_close(disc);
    return fflush(out) == 0 ? 0 : 1;
}

// MARK: - Sidecar transport stream

typedef struct {
    uint16_t pid;
    uint8_t stream_type;
    char language[4];
    uint8_t continuity;
    int core_index;
} SidecarStream;

typedef struct {
    FILE *file;
    SidecarStream streams[MAX_SIDECAR_STREAMS];
    size_t count;
    uint8_t pat_continuity;
    uint8_t pmt_continuity;
    bool anchor_written;
    bool failed;
} Sidecar;

static uint32_t mpeg_crc32(const uint8_t *data, size_t length) {
    uint32_t crc = 0xffffffff;
    for (size_t index = 0; index < length; index++) {
        crc ^= (uint32_t)data[index] << 24;
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc & 0x80000000) != 0 ? (crc << 1) ^ 0x04c11db7 : crc << 1;
        }
    }
    return crc;
}

static void sidecar_write_packets(Sidecar *sidecar, uint16_t pid, uint8_t *continuity, const uint8_t *data, size_t length) {
    bool first = true;
    while (length > 0 && !sidecar->failed) {
        uint8_t packet[TS_PACKET_SIZE];
        size_t payload = length < TS_PAYLOAD_SIZE ? length : TS_PAYLOAD_SIZE;
        packet[0] = 0x47;
        packet[1] = (uint8_t)((first ? 0x40 : 0x00) | ((pid >> 8) & 0x1f));
        packet[2] = (uint8_t)(pid & 0xff);
        size_t offset = 4;
        if (payload < TS_PAYLOAD_SIZE) {
            // An adaptation field of stuffing bytes fills the last packet of a PES packet.
            size_t adaptation_length = TS_PAYLOAD_SIZE - payload - 1;
            packet[3] = (uint8_t)(0x30 | *continuity);
            packet[4] = (uint8_t)adaptation_length;
            if (adaptation_length > 0) {
                packet[5] = 0x00;
                memset(packet + 6, 0xff, adaptation_length - 1);
            }
            offset = 5 + adaptation_length;
        } else {
            packet[3] = (uint8_t)(0x10 | *continuity);
        }
        memcpy(packet + offset, data, payload);
        *continuity = (*continuity + 1) & 0x0f;
        if (fwrite(packet, 1, TS_PACKET_SIZE, sidecar->file) != TS_PACKET_SIZE) {
            sidecar->failed = true;
        }
        data += payload;
        length -= payload;
        first = false;
    }
}

static void sidecar_write_section(Sidecar *sidecar, uint16_t pid, uint8_t *continuity, uint8_t *section, size_t length) {
    uint32_t crc = mpeg_crc32(section, length - 4);
    section[length - 4] = (uint8_t)(crc >> 24);
    section[length - 3] = (uint8_t)(crc >> 16);
    section[length - 2] = (uint8_t)(crc >> 8);
    section[length - 1] = (uint8_t)crc;
    // A pointer field leads the section; 0xff bytes pad the last packet.
    uint8_t buffer[1 + 1024 + TS_PAYLOAD_SIZE];
    buffer[0] = 0x00;
    memcpy(buffer + 1, section, length);
    size_t total = 1 + length;
    size_t padded = ((total + TS_PAYLOAD_SIZE - 1) / TS_PAYLOAD_SIZE) * TS_PAYLOAD_SIZE;
    memset(buffer + total, 0xff, padded - total);
    for (size_t offset = 0; offset < padded && !sidecar->failed; offset += TS_PAYLOAD_SIZE) {
        uint8_t packet[TS_PACKET_SIZE];
        packet[0] = 0x47;
        packet[1] = (uint8_t)((offset == 0 ? 0x40 : 0x00) | ((pid >> 8) & 0x1f));
        packet[2] = (uint8_t)(pid & 0xff);
        packet[3] = (uint8_t)(0x10 | *continuity);
        memcpy(packet + 4, buffer + offset, TS_PAYLOAD_SIZE);
        *continuity = (*continuity + 1) & 0x0f;
        if (fwrite(packet, 1, TS_PACKET_SIZE, sidecar->file) != TS_PACKET_SIZE) {
            sidecar->failed = true;
        }
    }
}

static bool language_valid(const char *language) {
    for (int index = 0; index < 3; index++) {
        if (language[index] < 'a' || language[index] > 'z') {
            return false;
        }
    }
    return true;
}

static bool sidecar_write_tables(Sidecar *sidecar) {
    uint8_t pat[16] = {
        0x00, 0xb0, 13, 0x00, 0x01, 0xc1, 0x00, 0x00, 0x00, 0x01, (uint8_t)(0xe0 | (PMT_PID >> 8)), PMT_PID & 0xff,
    };
    sidecar_write_section(sidecar, 0x0000, &sidecar->pat_continuity, pat, 16);

    uint8_t pmt[1024];
    size_t length = 12;
    // The HDMV registration lets FFmpeg read the Blu-ray stream types.
    const uint8_t registration[] = {0x05, 0x04, 'H', 'D', 'M', 'V'};
    memcpy(pmt + length, registration, sizeof(registration));
    length += sizeof(registration);
    for (size_t index = 0; index < sidecar->count; index++) {
        const SidecarStream *stream = &sidecar->streams[index];
        bool has_language = language_valid(stream->language);
        if (length + 11 + 4 > sizeof(pmt)) {
            return false;
        }
        pmt[length++] = stream->stream_type;
        pmt[length++] = (uint8_t)(0xe0 | (stream->pid >> 8));
        pmt[length++] = (uint8_t)(stream->pid & 0xff);
        pmt[length++] = 0xf0;
        pmt[length++] = has_language ? 6 : 0;
        if (has_language) {
            pmt[length++] = 0x0a;
            pmt[length++] = 0x04;
            memcpy(pmt + length, stream->language, 3);
            length += 3;
            pmt[length++] = 0x00;
        }
    }
    length += 4;
    size_t section_length = length - 3;
    pmt[0] = 0x02;
    pmt[1] = (uint8_t)(0xb0 | (section_length >> 8));
    pmt[2] = (uint8_t)(section_length & 0xff);
    pmt[3] = 0x00;
    pmt[4] = 0x01;
    pmt[5] = 0xc1;
    pmt[6] = 0x00;
    pmt[7] = 0x00;
    pmt[8] = 0xff;
    pmt[9] = 0xff;
    pmt[10] = 0xf0;
    pmt[11] = sizeof(registration);
    sidecar_write_section(sidecar, PMT_PID, &sidecar->pmt_continuity, pmt, length);
    return !sidecar->failed;
}

static SidecarStream *sidecar_stream(Sidecar *sidecar, uint16_t pid) {
    for (size_t index = 0; index < sidecar->count; index++) {
        if (sidecar->streams[index].pid == pid) {
            return &sidecar->streams[index];
        }
    }
    return NULL;
}

/// Writes the DTS core of a DTS-HD PES packet as its own PES packet, as MakeMKV keeps it.
static void sidecar_write_dts_core(Sidecar *sidecar, SidecarStream *core, const uint8_t *data, size_t length) {
    if (length < 9) {
        return;
    }
    size_t header_length = 9u + data[8];
    if (header_length + 8 > length) {
        return;
    }
    const uint8_t *payload = data + header_length;
    if (payload[0] != 0x7f || payload[1] != 0xfe || payload[2] != 0x80 || payload[3] != 0x01) {
        return;
    }
    size_t frame_size = ((((size_t)payload[5] & 0x03) << 12) | ((size_t)payload[6] << 4) | (payload[7] >> 4)) + 1;
    if (header_length + frame_size > length || header_length + frame_size - 6 > 0xffff) {
        return;
    }
    uint8_t header[9 + 255];
    memcpy(header, data, header_length);
    size_t packet_length = header_length + frame_size - 6;
    header[4] = (uint8_t)(packet_length >> 8);
    header[5] = (uint8_t)(packet_length & 0xff);
    uint8_t *packet = malloc(header_length + frame_size);
    if (packet == NULL) {
        sidecar->failed = true;
        return;
    }
    memcpy(packet, header, header_length);
    memcpy(packet + header_length, payload, frame_size);
    sidecar_write_packets(sidecar, core->pid, &core->continuity, packet, header_length + frame_size);
    free(packet);
}

static void encode_timestamp(uint8_t *output, uint8_t prefix, uint64_t timestamp) {
    output[0] = (uint8_t)((prefix << 4) | (((timestamp >> 30) & 0x07) << 1) | 1);
    output[1] = (uint8_t)((timestamp >> 22) & 0xff);
    output[2] = (uint8_t)((((timestamp >> 15) & 0x7f) << 1) | 1);
    output[3] = (uint8_t)((timestamp >> 7) & 0xff);
    output[4] = (uint8_t)(((timestamp & 0x7f) << 1) | 1);
}

/// Writes the first base-view access unit, the video timing anchor, as one PES packet.
static void sidecar_write_anchor(Sidecar *sidecar, const bd3d_packet *packet) {
    SidecarStream *anchor = &sidecar->streams[0];
    size_t length = 19 + packet->length;
    uint8_t *pes = malloc(length);
    if (pes == NULL) {
        sidecar->failed = true;
        return;
    }
    const uint8_t header[9] = {0x00, 0x00, 0x01, 0xe0, 0x00, 0x00, 0x80, 0xc0, 0x0a};
    memcpy(pes, header, sizeof(header));
    encode_timestamp(pes + 9, 0x03, packet->pts);
    encode_timestamp(pes + 14, 0x01, packet->dts);
    memcpy(pes + 19, packet->data, packet->length);
    sidecar_write_packets(sidecar, anchor->pid, &anchor->continuity, pes, length);
    free(pes);
    sidecar->anchor_written = true;
}

// MARK: - Stream

typedef struct {
    Sidecar *sidecar;
    uint64_t maximum_pairs;
    uint64_t pairs;
    bool write_failed;
} StreamContext;

static int emit_packet(void *opaque, const bd3d_packet *packet) {
    StreamContext *context = opaque;
    if (packet->kind == BD3D_PACKET_VIDEO) {
        if (context->sidecar != NULL && !context->sidecar->anchor_written) {
            sidecar_write_anchor(context->sidecar, packet);
            if (context->sidecar->failed) {
                context->write_failed = true;
                return 1;
            }
        }
        if (!write_all(STDOUT_FILENO, packet->data, packet->length) ||
            !write_all(STDOUT_FILENO, packet->dependent, packet->dependent_length)) {
            context->write_failed = true;
            return 1;
        }
        context->pairs++;
        return context->maximum_pairs > 0 && context->pairs >= context->maximum_pairs ? 1 : 0;
    }
    Sidecar *sidecar = context->sidecar;
    SidecarStream *stream = sidecar != NULL ? sidecar_stream(sidecar, packet->pid) : NULL;
    if (stream == NULL) {
        return 0;
    }
    sidecar_write_packets(sidecar, stream->pid, &stream->continuity, packet->data, packet->length);
    if (stream->core_index >= 0) {
        sidecar_write_dts_core(sidecar, &sidecar->streams[stream->core_index], packet->data, packet->length);
    }
    if (sidecar->failed) {
        context->write_failed = true;
        return 1;
    }
    return 0;
}

static bool parse_unsigned(const char *value, uint64_t maximum, uint64_t *parsed) {
    if (value == NULL || *value == '\0') {
        return false;
    }
    char *end = NULL;
    errno = 0;
    unsigned long long number = strtoull(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || number > maximum) {
        return false;
    }
    *parsed = number;
    return true;
}

static bool parse_pids(const char *value, uint16_t *pids, size_t *count) {
    *count = 0;
    if (value == NULL || *value == '\0') {
        return true;
    }
    const char *cursor = value;
    while (*cursor != '\0') {
        char *end = NULL;
        unsigned long pid = strtoul(cursor, &end, 10);
        if (end == cursor || pid == 0 || pid > 0x1ffe || *count == BD3D_MAX_SELECTED_PIDS) {
            return false;
        }
        pids[(*count)++] = (uint16_t)pid;
        if (*end == ',') {
            end++;
        } else if (*end != '\0') {
            return false;
        }
        cursor = end;
    }
    return true;
}

static const bd3d_stream *title_stream(const bd3d_title *title, uint16_t pid, bool audio) {
    const bd3d_stream *streams = audio ? title->audio : title->subtitles;
    uint8_t count = audio ? title->audio_count : title->subtitle_count;
    for (uint8_t index = 0; index < count; index++) {
        if (streams[index].pid == pid) {
            return &streams[index];
        }
    }
    return NULL;
}

/// Finds the listed title for a playlist. Several playlists can share a clip with different
/// streams or coverage, so the caller names the exact playlist that `titles` reported.
static bool find_playlist_title(bd3d_disc *disc, uint32_t playlist, bd3d_title *found) {
    bd3d_disc_info info = bd3d_disc_get_info(disc);
    for (uint32_t index = 0; index < info.title_count; index++) {
        if (bd3d_disc_title(disc, index, found) && found->playlist == playlist) {
            return true;
        }
    }
    return false;
}

static bool add_sidecar_stream(Sidecar *sidecar, const bd3d_stream *source, bool audio) {
    if (sidecar->count >= MAX_SIDECAR_STREAMS) {
        return false;
    }
    SidecarStream *stream = &sidecar->streams[sidecar->count++];
    *stream = (SidecarStream){.pid = source->pid, .stream_type = source->coding_type, .core_index = -1};
    memcpy(stream->language, source->language, 4);
    bool dts_hd = source->coding_type == CODING_TYPE_DTS_HD_HRA || source->coding_type == CODING_TYPE_DTS_HD_MA;
    if (!audio || !dts_hd) {
        return true;
    }
    if (sidecar->count >= MAX_SIDECAR_STREAMS) {
        return false;
    }
    stream->core_index = (int)sidecar->count;
    SidecarStream *core = &sidecar->streams[sidecar->count++];
    *core = (SidecarStream){
        .pid = (uint16_t)(source->pid + DTS_CORE_PID_OFFSET), .stream_type = CODING_TYPE_DTS, .core_index = -1
    };
    memcpy(core->language, source->language, 4);
    return true;
}

static int stream_title(
    const char *path,
    uint32_t playlist,
    const char *sidecar_path,
    const uint16_t *audio_pids,
    size_t audio_count,
    const uint16_t *subtitle_pids,
    size_t subtitle_count,
    uint64_t maximum_pairs
) {
    SourceCheck check = check_source(path);
    if (!check.readable || check.encrypted) {
        return refuse_unreadable(&check);
    }
    bd3d_disc *disc = bd3d_disc_open(path);
    if (disc == NULL) {
        return print_error("open_failed", "libbluray could not open the source.");
    }
    bd3d_title title;
    if (!find_playlist_title(disc, playlist, &title) || !title.ssif_available || !title.clips_readable) {
        bd3d_disc_close(disc);
        return print_error("title_unreadable", "The title's clips cannot be read as one 3D title.");
    }
    Sidecar sidecar = {0};
    if (sidecar_path != NULL) {
        sidecar.streams[sidecar.count++] = (SidecarStream){
            .pid = BD3D_BASE_VIEW_PID, .stream_type = STREAM_TYPE_H264, .core_index = -1
        };
        for (size_t index = 0; index < audio_count + subtitle_count; index++) {
            bool audio = index < audio_count;
            uint16_t pid = audio ? audio_pids[index] : subtitle_pids[index - audio_count];
            const bd3d_stream *stream = title_stream(&title, pid, audio);
            if (stream == NULL || !add_sidecar_stream(&sidecar, stream, audio)) {
                bd3d_disc_close(disc);
                return print_error("invalid_stream", "A selected stream is not in the title.");
            }
        }
        sidecar.file = fopen(sidecar_path, "wb");
        if (sidecar.file == NULL) {
            bd3d_disc_close(disc);
            return print_error("sidecar_failed", "The audio and subtitle file could not be created.");
        }
        setvbuf(sidecar.file, NULL, _IOFBF, 1 << 20);
        if (!sidecar_write_tables(&sidecar)) {
            fclose(sidecar.file);
            bd3d_disc_close(disc);
            return print_error("sidecar_failed", "The audio and subtitle file could not be written.");
        }
    }
    StreamContext context = {
        .sidecar = sidecar_path != NULL ? &sidecar : NULL,
        .maximum_pairs = maximum_pairs,
    };
    bd3d_stats stats = {0};
    bd3d_result result = bd3d_disc_stream(
        disc,
        title.playlist,
        0,
        sidecar_path != NULL ? audio_pids : NULL,
        sidecar_path != NULL ? audio_count : 0,
        sidecar_path != NULL ? subtitle_pids : NULL,
        sidecar_path != NULL ? subtitle_count : 0,
        emit_packet,
        &context,
        &stats
    );
    bd3d_disc_close(disc);
    bool sidecar_closed = sidecar.file == NULL || fclose(sidecar.file) == 0;
    if (context.write_failed || !sidecar_closed) {
        return print_error("write_failed", "The video or the audio and subtitle output could not be written.");
    }
    if (result != BD3D_OK && result != BD3D_STOPPED) {
        return print_error(result_code(result), "The 3D video could not be read from the source.");
    }
    fprintf(
        stderr,
        "{\"type\":\"bd3d.stream.complete\",\"video_pairs\":%" PRIu64 ",\"audio_packets\":%" PRIu64
        ",\"subtitle_packets\":%" PRIu64 ",\"bytes\":%" PRIu64 "}\n",
        stats.video_pairs,
        stats.audio_packets,
        stats.subtitle_packets,
        stats.bytes
    );
    return 0;
}

// MARK: - Excerpt

static int write_excerpt(const char *path, uint32_t playlist, uint64_t start_seconds, uint64_t seconds) {
    SourceCheck check = check_source(path);
    if (!check.readable || check.encrypted) {
        return refuse_unreadable(&check);
    }
    bd_set_debug_mask(0);
    BLURAY *bluray = bd_open(path, NULL);
    if (bluray == NULL) {
        return print_error("open_failed", "libbluray could not open the source.");
    }
    if (!bd_select_playlist(bluray, playlist)) {
        bd_close(bluray);
        return print_error("playlist_unavailable", "The playlist could not be selected.");
    }
    uint64_t start = start_seconds * BD_TICKS_PER_SECOND;
    uint64_t end = (start_seconds + seconds) * BD_TICKS_PER_SECOND;
    if (start > 0 && bd_seek_time(bluray, start) < 0) {
        bd_close(bluray);
        return print_error("seek_failed", "The playlist could not be positioned.");
    }
    uint8_t *buffer = malloc(EXCERPT_READ_SIZE);
    int status = buffer != NULL ? 0 : print_error("out_of_memory", "The read buffer could not be allocated.");
    while (status == 0 && bd_tell_time(bluray) < end) {
        int count = bd_read(bluray, buffer, EXCERPT_READ_SIZE);
        if (count < 0) {
            status = print_error("read_failed", "The playlist could not be read.");
        } else if (count == 0) {
            break;
        } else if (!write_all(STDOUT_FILENO, buffer, (size_t)count)) {
            status = print_error("write_failed", "The excerpt could not be written.");
        }
    }
    free(buffer);
    bd_close(bluray);
    return status;
}

// MARK: - Main

static int print_usage(void) {
    fputs(
        "Usage:\n"
        "  bd3d_reader titles <source>\n"
        "  bd3d_reader stream <source> <playlist> [--sidecar <path>] [--audio <pid,...>] [--subtitles <pid,...>]"
        " [--maximum-pairs <count>]\n"
        "  bd3d_reader excerpt <source> <playlist> <start-seconds> <seconds>\n"
        "  bd3d_reader --version\n",
        stderr
    );
    return 2;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        printf("bd3d_reader contract %d\n", CONTRACT_VERSION);
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "titles") == 0) {
        return list_titles(argv[2]);
    }
    if (argc >= 4 && strcmp(argv[1], "stream") == 0) {
        const char *sidecar_path = NULL;
        uint16_t audio_pids[BD3D_MAX_SELECTED_PIDS];
        uint16_t subtitle_pids[BD3D_MAX_SELECTED_PIDS];
        size_t audio_count = 0;
        size_t subtitle_count = 0;
        uint64_t maximum_pairs = 0;
        for (int index = 4; index < argc; index += 2) {
            const char *value = index + 1 < argc ? argv[index + 1] : NULL;
            bool valid = value != NULL;
            if (valid && strcmp(argv[index], "--sidecar") == 0) {
                sidecar_path = value;
            } else if (valid && strcmp(argv[index], "--audio") == 0) {
                valid = parse_pids(value, audio_pids, &audio_count);
            } else if (valid && strcmp(argv[index], "--subtitles") == 0) {
                valid = parse_pids(value, subtitle_pids, &subtitle_count);
            } else if (valid && strcmp(argv[index], "--maximum-pairs") == 0) {
                valid = parse_unsigned(value, UINT32_MAX, &maximum_pairs);
            } else {
                valid = false;
            }
            if (!valid) {
                return print_usage();
            }
        }
        if (audio_count + subtitle_count > BD3D_MAX_SELECTED_PIDS) {
            return print_error("too_many_pids", "Too many audio and subtitle streams were selected.");
        }
        uint64_t playlist = 0;
        if (!parse_unsigned(argv[3], 99999, &playlist)) {
            return print_usage();
        }
        return stream_title(
            argv[2], (uint32_t)playlist, sidecar_path, audio_pids, audio_count, subtitle_pids, subtitle_count, maximum_pairs
        );
    }
    if (argc == 6 && strcmp(argv[1], "excerpt") == 0) {
        uint64_t playlist = 0;
        uint64_t start_seconds = 0;
        uint64_t seconds = 0;
        if (!parse_unsigned(argv[3], 99999, &playlist) || !parse_unsigned(argv[4], 86400, &start_seconds) ||
            !parse_unsigned(argv[5], 3600, &seconds) || seconds == 0) {
            return print_usage();
        }
        return write_excerpt(argv[2], (uint32_t)playlist, start_seconds, seconds);
    }
    return print_usage();
}

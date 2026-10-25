// Narrow wrapper around FFmpeg's LGPL `dca` decoder. Licensed LGPL-2.1-or-later, like the
// FFmpeg libraries it is linked with inside FFmpegDCA.framework.

#include "FFmpegDCA.h"

#include <stdlib.h>
#include <string.h>

#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libavutil/error.h>
#include <libavutil/ffversion.h>
#include <libavutil/opt.h>

struct BDAVPDCADecoder {
    AVCodecContext *context;
    AVPacket *packet;
    AVFrame *frame;
};

BDAVPDCADecoder *bdavp_dca_create(void) {
    const AVCodec *codec = avcodec_find_decoder(AV_CODEC_ID_DTS);
    if (codec == NULL) {
        return NULL;
    }
    BDAVPDCADecoder *decoder = calloc(1, sizeof(*decoder));
    if (decoder == NULL) {
        return NULL;
    }
    decoder->context = avcodec_alloc_context3(codec);
    decoder->packet = av_packet_alloc();
    decoder->frame = av_frame_alloc();
    if (decoder->context == NULL || decoder->packet == NULL || decoder->frame == NULL) {
        bdavp_dca_destroy(decoder);
        return NULL;
    }
    AVDictionary *options = NULL;
    av_dict_set(&options, "core_only", "1", 0);
    int result = avcodec_open2(decoder->context, codec, &options);
    av_dict_free(&options);
    if (result < 0) {
        bdavp_dca_destroy(decoder);
        return NULL;
    }
    return decoder;
}

void bdavp_dca_destroy(BDAVPDCADecoder *decoder) {
    if (decoder == NULL) {
        return;
    }
    av_frame_free(&decoder->frame);
    av_packet_free(&decoder->packet);
    avcodec_free_context(&decoder->context);
    free(decoder);
}

static float sample_at(const AVFrame *frame, int channel, int index) {
    switch (frame->format) {
    case AV_SAMPLE_FMT_FLTP:
        return ((const float *)frame->extended_data[channel])[index];
    case AV_SAMPLE_FMT_S32P:
        return (float)((const int32_t *)frame->extended_data[channel])[index] / 2147483648.0f;
    case AV_SAMPLE_FMT_S16P:
        return (float)((const int16_t *)frame->extended_data[channel])[index] / 32768.0f;
    default:
        return 0.0f;
    }
}

int32_t bdavp_dca_decode(
    BDAVPDCADecoder *decoder,
    const uint8_t *data,
    int32_t size,
    float *output,
    int32_t output_capacity,
    BDAVPDCAFrameInfo *info
) {
    if (decoder == NULL || data == NULL || size <= 0 || output == NULL || info == NULL) {
        return AVERROR(EINVAL);
    }
    memset(info, 0, sizeof(*info));
    int result = av_new_packet(decoder->packet, size);
    if (result < 0) {
        return result;
    }
    memcpy(decoder->packet->data, data, (size_t)size);
    result = avcodec_send_packet(decoder->context, decoder->packet);
    av_packet_unref(decoder->packet);
    if (result < 0) {
        return result;
    }
    result = avcodec_receive_frame(decoder->context, decoder->frame);
    if (result == AVERROR(EAGAIN)) {
        return 1;
    }
    if (result < 0) {
        return result;
    }
    const AVFrame *frame = decoder->frame;
    int channels = frame->ch_layout.nb_channels;
    int samples = frame->nb_samples;
    if (channels <= 0 || samples <= 0) {
        av_frame_unref(decoder->frame);
        return 1;
    }
    if ((int64_t)channels * samples > output_capacity) {
        av_frame_unref(decoder->frame);
        return AVERROR(ENOSPC);
    }
    if (frame->format != AV_SAMPLE_FMT_FLTP && frame->format != AV_SAMPLE_FMT_S32P &&
        frame->format != AV_SAMPLE_FMT_S16P) {
        av_frame_unref(decoder->frame);
        return AVERROR(ENOSYS);
    }
    for (int index = 0; index < samples; index++) {
        for (int channel = 0; channel < channels; channel++) {
            output[index * channels + channel] = sample_at(frame, channel, index);
        }
    }
    info->samples_per_channel = samples;
    info->channels = channels;
    info->sample_rate = frame->sample_rate;
    info->channel_mask = frame->ch_layout.order == AV_CHANNEL_ORDER_NATIVE ? frame->ch_layout.u.mask : 0;
    av_frame_unref(decoder->frame);
    return 0;
}

void bdavp_dca_flush(BDAVPDCADecoder *decoder) {
    if (decoder != NULL && decoder->context != NULL) {
        avcodec_flush_buffers(decoder->context);
    }
}

const char *bdavp_dca_ffmpeg_version(void) {
    return FFMPEG_VERSION;
}

const char *bdavp_dca_license(void) {
    return avcodec_license();
}

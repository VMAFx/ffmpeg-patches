/*
 * dv5gen: write a synthetic Dolby Vision profile 5 HEVC clip with known
 * reshape curves, for parity tests of FFmpeg's tone-map filters.
 *
 * The base layer is arbitrary 10-bit full-range content; no inverse
 * transform is needed, because the reference output is whatever
 * vf_libplacebo makes of the same RPUs. Colour matrices are the profile 5
 * defaults of dovi_tool (dolby_vision/src/rpu/profiles/profile5.rs, MIT).
 *
 * SPDX-FileCopyrightText: 2026 Lusoris
 * SPDX-License-Identifier: LGPL-2.1-or-later
 *
 * usage: dv5gen out.mp4 [frames] [width] [height]
 *
 * dv5.mp4 next to this file was written with FFmpeg n9.0.2 and x265 4.3:
 *
 *     cc -O2 -Wall -Wextra -o dv5gen dv5gen.c -lavformat -lavcodec -lavutil
 *     ./dv5gen dv5.mp4 96
 *
 * 1280x720, 96 frames, 24 fps; the luma box level and the L1 maximum change
 * every frame, so a filter that reads only the first RPU shows up.
 */

#include <errno.h>
#include <stdlib.h>

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/dovi_meta.h>
#include <libavutil/frame.h>
#include <libavutil/log.h>
#include <libavutil/opt.h>

enum {
    DENOM_LOG2 = 23,
    MAX_FRAMES = 100000,
    DECIMAL = 10,
    MAX_SIDE = 16384,
    MIN_SIDE = 64,
    SIDE_ALIGN = 8,
    DEFAULT_FRAMES = 48,
    DEFAULT_WIDTH = 1280,
    DEFAULT_HEIGHT = 720,
    FPS = 24,
    CODE_MAX = 1023,
    I_SPLIT = 400,
    MMR_TERMS = 7,
    NUM_COEFS = 9,
    YCC_DENOM = 8192,
    LMS_DENOM = 16384,
    BOX_BASE = 700,
    BOX_STEP = 37,
    BOX_RANGE = 300,
    BOX_SPEED = 16,
    PQ_PER_CODE = 4,
    L1_AVG_PQ = 1400,
    RAMP_BASE = 64,
    RAMP_SPAN = 600,
    CHROMA_BASE = 432,
    CHROMA_SPAN = 160,
};

typedef struct Gen {
    AVFormatContext *fmt;
    AVCodecContext *enc;
    AVStream *st;
    AVPacket *pkt;
    int frames;
    int width;
    int height;
} Gen;

/* The RPU header of profile 5: no enhancement layer, full-range base layer. */
static const AVDOVIRpuDataHeader HEADER = {
    .rpu_type = 2,
    .rpu_format = 18,
    .vdr_rpu_profile = 0,
    .vdr_rpu_level = 0,
    .coef_data_type = 0,
    .coef_log2_denom = DENOM_LOG2,
    .vdr_rpu_normalized_idc = 1,
    .bl_video_full_range_flag = 1,
    .bl_bit_depth = 10,
    .el_bit_depth = 10,
    .vdr_bit_depth = 12,
    .disable_residual_flag = 1,
};

/* I: two quadratic pieces split at I_SPLIT. P: one second-order MMR piece
 * with cross terms. T: identity line. Expected values for tests follow from
 * these numbers. */
static const double I_POLY[2][3] = { { 0.0, 1.10, -0.12 }, { 0.03, 0.95, 0.02 } };
static const double P_MMR_CONSTANT = -0.01;
static const double P_MMR1[MMR_TERMS] = { 0.02, 0.97, 0.01, 0.03, -0.02, 0.0, 0.01 };
static const double P_MMR2_TERM1 = 0.02;

/* Profile 5 matrices in the RPU's fixed point (Q13 and Q14). */
static const int YCC[NUM_COEFS] = { 8192, 799, 1681, 8192, -933, 1091, 8192, 267, -5545 };
static const int LMS[NUM_COEFS] = { 17081, -349, -349, -349, 17081, -349, -349, -349, 17081 };

static const AVDOVIColorMetadata COLOR_TAIL = {
    .signal_eotf = 65535,
    .signal_bit_depth = 12,
    .signal_color_space = 2,
    .signal_full_range_flag = 1,
    .source_min_pq = 62,
    .source_max_pq = 3696,
    .source_diagonal = 42,
};

static int64_t fx(double v)
{
    const double one = (double)(INT64_C(1) << DENOM_LOG2);
    const double half = 0.5;

    return (int64_t)((v * one) + (v < 0 ? -half : half));
}

static void set_curves(AVDOVIDataMapping *m)
{
    AVDOVIReshapingCurve *c = m->curves;

    m->num_x_partitions = 1;
    m->num_y_partitions = 1;
    m->nlq_method_idc = AV_DOVI_NLQ_NONE;
    c[0].num_pivots = 3;
    c[0].pivots[1] = I_SPLIT;
    c[0].pivots[2] = CODE_MAX;
    for (int i = 0; i < 2; i++) {
        c[0].mapping_idc[i] = AV_DOVI_MAPPING_POLYNOMIAL;
        c[0].poly_order[i] = 2;
        for (int k = 0; k < 3; k++) {
            c[0].poly_coef[i][k] = fx(I_POLY[i][k]);
        }
    }
    c[1].num_pivots = 2;
    c[1].pivots[1] = CODE_MAX;
    c[1].mapping_idc[0] = AV_DOVI_MAPPING_MMR;
    c[1].mmr_order[0] = 2;
    c[1].mmr_constant[0] = fx(P_MMR_CONSTANT);
    for (int k = 0; k < MMR_TERMS; k++) {
        c[1].mmr_coef[0][0][k] = fx(P_MMR1[k]);
    }
    c[1].mmr_coef[0][1][1] = fx(P_MMR2_TERM1);
    c[2].num_pivots = 2;
    c[2].pivots[1] = CODE_MAX;
    c[2].mapping_idc[0] = AV_DOVI_MAPPING_POLYNOMIAL;
    c[2].poly_order[0] = 1;
    c[2].poly_coef[0][1] = fx(1.0);
}

static void set_color(AVDOVIColorMetadata *c)
{
    *c = COLOR_TAIL;
    for (int i = 0; i < NUM_COEFS; i++) {
        c->ycc_to_rgb_matrix[i] = av_make_q(YCC[i], YCC_DENOM);
        c->rgb_to_lms_matrix[i] = av_make_q(LMS[i], LMS_DENOM);
    }
    c->ycc_to_rgb_offset[0] = av_make_q(0, 1);
    c->ycc_to_rgb_offset[1] = av_make_q(1, 2);
    c->ycc_to_rgb_offset[2] = av_make_q(1, 2);
}

/* Brightness of the moving box, in 10-bit code values; it varies per frame
 * so that dynamic peak detection has something to follow. */
static int box_level(int n)
{
    return BOX_BASE + ((n * BOX_STEP) % BOX_RANGE);
}

/* The metadata of frame n, wrapped in a buffer that owns it. */
static AVBufferRef *metadata_buf(int n)
{
    size_t size = 0;
    AVDOVIMetadata *md = av_dovi_metadata_alloc(&size);
    AVDOVIDmData *l1 = NULL;
    AVBufferRef *buf = NULL;

    if (!md) {
        return NULL;
    }
    *av_dovi_get_header(md) = HEADER;
    set_curves(av_dovi_get_mapping(md));
    set_color(av_dovi_get_color(md));
    md->num_ext_blocks = 1;
    l1 = av_dovi_get_ext(md, 0);
    l1->level = 1;
    l1->l1.min_pq = 0;
    l1->l1.max_pq = (uint16_t)(box_level(n) * PQ_PER_CODE);
    l1->l1.avg_pq = L1_AVG_PQ;
    buf = av_buffer_create((uint8_t *)md, size, NULL, NULL, 0);
    if (!buf) {
        av_free(md);
    }
    return buf;
}

static uint16_t *plane_row(const AVFrame *f, int plane, int y)
{
    return (uint16_t *)(f->data[plane] + ((ptrdiff_t)y * f->linesize[plane]));
}

static void fill_planes(const AVFrame *f, int n)
{
    const int bx = (n * BOX_SPEED) % (f->width / 2);
    const int by = f->height / 4;

    for (int y = 0; y < f->height; y++) {
        uint16_t *row = plane_row(f, 0, y);
        const int in_rows = y >= by && y < by + (f->height / 2);
        for (int x = 0; x < f->width; x++) {
            const int in_box = in_rows && x >= bx && x < bx + (f->width / 4);
            row[x] = (uint16_t)(in_box ? box_level(n) : RAMP_BASE + ((x * RAMP_SPAN) / f->width));
        }
    }
    for (int y = 0; y < f->height / 2; y++) {
        uint16_t *u = plane_row(f, 1, y);
        uint16_t *v = plane_row(f, 2, y);
        for (int x = 0; x < f->width / 2; x++) {
            u[x] = (uint16_t)(CHROMA_BASE + ((y * CHROMA_SPAN) / (f->height / 2)));
            v[x] = (uint16_t)(CHROMA_BASE + ((x * CHROMA_SPAN) / (f->width / 2)));
        }
    }
}

static int attach_metadata(AVFrame *f, int n)
{
    AVBufferRef *buf = metadata_buf(n);

    if (!buf) {
        return AVERROR(ENOMEM);
    }
    if (!av_frame_new_side_data_from_buf(f, AV_FRAME_DATA_DOVI_METADATA, buf)) {
        av_buffer_unref(&buf);
        return AVERROR(ENOMEM);
    }
    return 0;
}

/* The encoder decides the profile from the header it finds here
 * (ff_dovi_configure: vdr_rpu_profile 0 + full range = profile 5). */
static int add_stream_side_data(AVCodecContext *enc)
{
    AVBufferRef *buf = metadata_buf(0);
    const AVFrameSideData *sd = NULL;

    if (!buf) {
        return AVERROR(ENOMEM);
    }
    sd = av_frame_side_data_add(&enc->decoded_side_data, &enc->nb_decoded_side_data,
                                AV_FRAME_DATA_DOVI_METADATA, &buf, 0);
    av_buffer_unref(&buf);
    return sd ? 0 : AVERROR(ENOMEM);
}

static int set_options(AVCodecContext *enc)
{
    int ret = av_opt_set(enc->priv_data, "dolbyvision", "1", 0);

    if (ret >= 0) {
        ret = av_opt_set(enc->priv_data, "preset", "fast", 0);
    }
    /* x265 refuses Dolby Vision without VBV ("requires VBV settings to
     * enable HRD"). */
    if (ret >= 0) {
        ret = av_opt_set(enc->priv_data, "x265-params",
                         "crf=12:vbv-maxrate=20000:vbv-bufsize=20000:log-level=error", 0);
    }
    return ret;
}

static int open_encoder(Gen *g)
{
    const AVCodec *codec = avcodec_find_encoder_by_name("libx265");
    int ret = 0;

    if (!codec) {
        return AVERROR_ENCODER_NOT_FOUND;
    }
    g->enc = avcodec_alloc_context3(codec);
    if (!g->enc) {
        return AVERROR(ENOMEM);
    }
    g->enc->width = g->width;
    g->enc->height = g->height;
    g->enc->pix_fmt = AV_PIX_FMT_YUV420P10;
    g->enc->time_base = (AVRational){ 1, FPS };
    g->enc->framerate = (AVRational){ FPS, 1 };
    g->enc->color_range = AVCOL_RANGE_JPEG;
    g->enc->gop_size = FPS;
    if (((unsigned)g->fmt->oformat->flags & (unsigned)AVFMT_GLOBALHEADER) != 0) {
        g->enc->flags = (int)((unsigned)g->enc->flags | (unsigned)AV_CODEC_FLAG_GLOBAL_HEADER);
    }
    ret = set_options(g->enc);
    if (ret >= 0) {
        ret = add_stream_side_data(g->enc);
    }
    if (ret >= 0) {
        ret = avcodec_open2(g->enc, codec, NULL);
    }
    if (ret >= 0) {
        ret = avcodec_parameters_from_context(g->st->codecpar, g->enc);
    }
    g->st->time_base = g->enc->time_base;
    return ret;
}

static int drain(Gen *g)
{
    int ret = 0;

    for (int i = 0; i < MAX_FRAMES && ret >= 0; i++) {
        ret = avcodec_receive_packet(g->enc, g->pkt);
        if (ret < 0) {
            break;
        }
        av_packet_rescale_ts(g->pkt, g->enc->time_base, g->st->time_base);
        g->pkt->stream_index = g->st->index;
        ret = av_interleaved_write_frame(g->fmt, g->pkt);
    }
    return (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) ? 0 : ret;
}

static int encode_frame(Gen *g, AVFrame *f, int n)
{
    int ret = 0;

    av_frame_unref(f);
    f->format = g->enc->pix_fmt;
    f->width = g->enc->width;
    f->height = g->enc->height;
    f->color_range = AVCOL_RANGE_JPEG;
    f->pts = n;
    ret = av_frame_get_buffer(f, 0);
    if (ret >= 0) {
        fill_planes(f, n);
        ret = attach_metadata(f, n);
    }
    if (ret >= 0) {
        ret = avcodec_send_frame(g->enc, f);
    }
    return ret >= 0 ? drain(g) : ret;
}

static int encode_all(Gen *g)
{
    AVFrame *f = av_frame_alloc();
    int ret = f ? 0 : AVERROR(ENOMEM);

    for (int n = 0; n < g->frames && ret >= 0; n++) {
        ret = encode_frame(g, f, n);
    }
    av_frame_free(&f);
    if (ret >= 0) {
        ret = avcodec_send_frame(g->enc, NULL);
    }
    return ret >= 0 ? drain(g) : ret;
}

static int run(Gen *g, const char *out)
{
    int ret = avformat_alloc_output_context2(&g->fmt, NULL, NULL, out);

    if (ret < 0) {
        return ret;
    }
    /* dvcC/dvvC is written only at unofficial compliance (movenc.c) */
    g->fmt->strict_std_compliance = FF_COMPLIANCE_UNOFFICIAL;
    g->st = avformat_new_stream(g->fmt, NULL);
    g->pkt = av_packet_alloc();
    if (!g->st || !g->pkt) {
        return AVERROR(ENOMEM);
    }
    ret = open_encoder(g);
    if (ret >= 0) {
        ret = avio_open(&g->fmt->pb, out, AVIO_FLAG_WRITE);
    }
    if (ret >= 0) {
        ret = avformat_write_header(g->fmt, NULL);
    }
    if (ret >= 0) {
        ret = encode_all(g);
    }
    if (ret >= 0) {
        ret = av_write_trailer(g->fmt);
    }
    return ret;
}

/* s as a whole decimal number in 1..MAX_FRAMES, or def when s is NULL; -1
 * when it is not one. */
static int int_arg(const char *s, int def)
{
    char *end = NULL;
    long v = 0;

    if (!s) {
        return def;
    }
    errno = 0;
    v = strtol(s, &end, DECIMAL);
    if (errno != 0 || end == s || *end != '\0' || v < 1 || v > MAX_FRAMES) {
        return -1;
    }
    return (int)v;
}

static int valid_side(int v)
{
    return v >= MIN_SIDE && v <= MAX_SIDE && v % SIDE_ALIGN == 0;
}

int main(int argc, char **argv)
{
    Gen g = { 0 };
    int ret = 0;

    if (argc < 2) {
        av_log(NULL, AV_LOG_ERROR, "usage: %s out.mp4 [frames] [width] [height]\n", argv[0]);
        return 2;
    }
    g.frames = int_arg(argc > 2 ? argv[2] : NULL, DEFAULT_FRAMES);
    g.width = int_arg(argc > 3 ? argv[3] : NULL, DEFAULT_WIDTH);
    g.height = int_arg(argc > 4 ? argv[4] : NULL, DEFAULT_HEIGHT);
    if (g.frames < 1 || !valid_side(g.width) || !valid_side(g.height)) {
        av_log(NULL, AV_LOG_ERROR, "frames must be 1..%d, sides %d..%d and a multiple of %d\n",
               MAX_FRAMES, MIN_SIDE, MAX_SIDE, SIDE_ALIGN);
        return 2;
    }
    ret = run(&g, argv[1]);
    if (g.fmt && g.fmt->pb) {
        avio_closep(&g.fmt->pb);
    }
    avcodec_free_context(&g.enc);
    avformat_free_context(g.fmt);
    av_packet_free(&g.pkt);
    if (ret < 0) {
        av_log(NULL, AV_LOG_ERROR, "dv5gen: %s\n", av_err2str(ret));
    }
    return ret < 0;
}

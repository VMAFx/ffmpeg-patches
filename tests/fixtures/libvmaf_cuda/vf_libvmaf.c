/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* SPDX-FileCopyrightText: 2017 Ronald S. Bultje <rsbultje@gmail.com> */
/* SPDX-FileCopyrightText: 2017 Ashish Pratap Singh <ashk43712@gmail.com> */
/*
 * Copyright (c) 2017 Ronald S. Bultje <rsbultje@gmail.com>
 * Copyright (c) 2017 Ashish Pratap Singh <ashk43712@gmail.com>
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * FFmpeg is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with FFmpeg; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

/**
 * @file
 * Calculate the VMAF between two input videos.
 */

/* Pinned n9.0.2 excerpts: 946fcce07b6dcd0331c8cc609192aeff5e1924f8. */
#if CONFIG_LIBVMAF_CUDA_FILTER
static const enum AVPixelFormat supported_formats[] = {
    AV_PIX_FMT_YUV420P,
    AV_PIX_FMT_YUV444P16,
};

static int copy_picture_data_cuda(VmafContext* vmaf,
                                  AVCUDADeviceContext* device_hwctx,
                                  AVFrame* src, VmafPicture* dst,
                                  enum AVPixelFormat pix_fmt)
{
    const AVPixFmtDescriptor *pix_desc = av_pix_fmt_desc_get(pix_fmt);
    CudaFunctions *cu = device_hwctx->internal->cuda_dl;

    CUDA_MEMCPY2D m = {
        .srcMemoryType = CU_MEMORYTYPE_DEVICE,
        .dstMemoryType = CU_MEMORYTYPE_DEVICE,
    };

    int err = vmaf_cuda_fetch_preallocated_picture(vmaf, dst);
    if (err)
        return AVERROR(ENOMEM);

    err = cu->cuCtxPushCurrent(device_hwctx->cuda_ctx);
    if (err)
        return AVERROR_EXTERNAL;

    for (unsigned i = 0; i < pix_desc->nb_components; i++) {
        m.srcDevice = (CUdeviceptr) src->data[i];
        m.srcPitch = src->linesize[i];
        m.dstDevice = (CUdeviceptr) dst->data[i];
        m.dstPitch = dst->stride[i];
        m.WidthInBytes = dst->w[i] * ((dst->bpc + 7) / 8);
        m.Height = dst->h[i];

        err = cu->cuMemcpy2D(&m);
        if (err)
            return AVERROR_EXTERNAL;
        break;
    }

    err = cu->cuCtxPopCurrent(NULL);
    if (err)
        return AVERROR_EXTERNAL;

    return 0;
}

static int do_vmaf_cuda(FFFrameSync* fs)
/* End of pinned excerpt; the harness compiles only the copy function above. */
#endif

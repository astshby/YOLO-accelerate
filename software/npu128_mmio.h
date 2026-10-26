/* SPDX-License-Identifier: Apache-2.0 */
#ifndef NPU128_MMIO_H
#define NPU128_MMIO_H
#include <stdint.h>

enum {
    NPU128_ID=0x000, NPU128_ABI=0x004, NPU128_CONTROL=0x008, NPU128_STATUS=0x00c,
    NPU128_ERRORS=0x010, NPU128_ACT_WORDS=0x014, NPU128_WEIGHT_WORDS=0x018,
    NPU128_OUTPUT_COUNT=0x01c, NPU128_SHAPE=0x020, NPU128_OPTIONS=0x024,
    NPU128_ACT_INDEX=0x028, NPU128_WEIGHT_INDEX=0x02c, NPU128_CFG_INDEX=0x030,
    NPU128_CFG_DATA=0x034, NPU128_ACT_COMMIT=0x038, NPU128_WEIGHT_COMMIT=0x03c,
    NPU128_CFG_COMMIT=0x040, NPU128_OUTPUT_POP=0x044,
    NPU128_PERF_MAC=0x048, NPU128_PERF_CONV=0x04c, NPU128_PERF_TOTAL=0x050,
    NPU128_PERF_POST=0x054, NPU128_OUTPUT_META=0x058, NPU128_OUTPUT_FLAGS=0x05c,
    NPU128_ACT_DATA=0x060, NPU128_BIAS=0x080, NPU128_WEIGHT_DATA=0x100,
    NPU128_OUTPUT_RAW=0x200, NPU128_OUTPUT_INT8=0x240,
    NPU128_START=1, NPU128_RESET=2, NPU128_BUSY=1, NPU128_OUTPUT_VALID=2,
    NPU128_DONE=4, NPU128_ERROR=8, NPU128_LUT_READY=16,
    NPU128_ERR_ADDRESS=1, NPU128_ERR_STATE=2, NPU128_ERR_RANGE=4,
    NPU128_ERR_COMMAND=8, NPU128_ERR_CONFIG=16
};

/* Offsets are byte addresses. Transport callbacks return 0 on success and a
 * negative value on failure. They must wait for the response, including errors.
 * One owner must serialize complete staging/commit and output-read/pop sequences.
 * Volatile/fences/cache policy belong to the eventual platform transport. */
typedef struct {
    void *context;
    int (*read32)(void *, uint32_t offset, uint32_t *value);
    int (*write32)(void *, uint32_t offset, uint32_t value, uint8_t strobes);
} npu128_io;

typedef struct {
    uint32_t raw[16];
    uint32_t quantized[4];
    uint32_t meta;  /* mask[15:0], x[23:16], y[31:24] */
    uint32_t flags; /* mode[1:0], last[2], valid[3] */
} npu128_output;

/* Packing helpers require field-width-valid inputs: width/height <256,
 * cin <65536; cout <32, mode <4, and Boolean flags. The core checks supported
 * shapes on START. These helpers do not silently mask invalid caller values. */
static inline uint32_t npu128_shape(unsigned width, unsigned height, unsigned cin) {
    return (uint32_t)width | ((uint32_t)height<<8) | ((uint32_t)cin<<16);
}
static inline uint32_t npu128_options(unsigned cout, unsigned kernel3, unsigned stride2,
                                     unsigned pad, unsigned mode, unsigned bank) {
    return (uint32_t)cout | ((uint32_t)kernel3<<5) | ((uint32_t)stride2<<6) |
           ((uint32_t)pad<<7) | ((uint32_t)mode<<8) | ((uint32_t)bank<<10);
}
static inline int npu128_write(const npu128_io *io, uint32_t offset, uint32_t value) {
    return io->write32(io->context, offset, value, 15);
}
static inline int npu128_activation(const npu128_io *io, unsigned bank, uint32_t index, uint64_t word) {
    int rc;
    if (bank>1 || index>=24576) return -2;
    if ((rc=npu128_write(io,NPU128_ACT_INDEX,index|((uint32_t)bank<<31)))<0) return rc;
    if ((rc=npu128_write(io,NPU128_ACT_DATA,(uint32_t)word))<0) return rc;
    if ((rc=npu128_write(io,NPU128_ACT_DATA+4,(uint32_t)(word>>32)))<0) return rc;
    return npu128_write(io,NPU128_ACT_COMMIT,1);
}
static inline int npu128_weight(const npu128_io *io, uint32_t index, const uint32_t words[32]) {
    int rc;
    unsigned i;
    if (index>=864) return -2;
    if ((rc=npu128_write(io,NPU128_WEIGHT_INDEX,index))<0) return rc;
    for (i=0;i<32;i++)
        if ((rc=npu128_write(io,NPU128_WEIGHT_DATA+i*4,words[i]))<0) return rc;
    return npu128_write(io,NPU128_WEIGHT_COMMIT,1);
}
/* Core configuration errors are sticky/asynchronous device errors, distinct
 * from a failed bus access. Clear old errors before configuring a new task. */
static inline int npu128_configure(const npu128_io *io, unsigned kind, unsigned address, uint32_t data) {
    int rc;
    uint32_t errors;
    if (kind>3 || address>255) return -2;
    if ((rc=npu128_write(io,NPU128_CFG_INDEX,(kind<<8)|address))<0) return rc;
    if ((rc=npu128_write(io,NPU128_CFG_DATA,data))<0) return rc;
    if ((rc=npu128_write(io,NPU128_CFG_COMMIT,1))<0) return rc;
    if ((rc=io->read32(io->context,NPU128_ERRORS,&errors))<0) return rc;
    return errors ? -3 : 0;
}
/* Return 1 for a complete, popped vector; 0 if none is ready; negative on
 * transport failure. A failed read leaves the vector pending for retry. */
static inline int npu128_read_output(const npu128_io *io, npu128_output *output) {
    uint32_t status;
    unsigned i;
    int rc;
    if ((rc=io->read32(io->context,NPU128_STATUS,&status))<0) return rc;
    if (!(status & NPU128_OUTPUT_VALID)) return 0;
    if ((rc=io->read32(io->context,NPU128_OUTPUT_META,&output->meta))<0) return rc;
    if ((rc=io->read32(io->context,NPU128_OUTPUT_FLAGS,&output->flags))<0) return rc;
    for (i=0;i<16;i++)
        if ((rc=io->read32(io->context,NPU128_OUTPUT_RAW+i*4,&output->raw[i]))<0) return rc;
    for (i=0;i<4;i++)
        if ((rc=io->read32(io->context,NPU128_OUTPUT_INT8+i*4,&output->quantized[i]))<0) return rc;
    if ((rc=npu128_write(io,NPU128_OUTPUT_POP,1))<0) return rc;
    return 1;
}
#endif

/* Compile the transport ABI as strict C11, separately from the C++ simulator. */
#include "../software/npu128_mmio.h"
int npu128_c11_probe(const npu128_io *io, const uint32_t weight[32], npu128_output *out) {
    int rc=npu128_activation(io,0,0,UINT64_C(0x807f010203040506));
    if (rc<0) return rc;
    if ((rc=npu128_weight(io,0,weight))<0) return rc;
    if ((rc=npu128_configure(io,0,0,1))<0) return rc;
    if ((rc=npu128_write(io,NPU128_SHAPE,npu128_shape(1,1,8)))<0) return rc;
    if ((rc=npu128_write(io,NPU128_OPTIONS,npu128_options(16,0,0,0,0,0)))<0) return rc;
    return npu128_read_output(io,out);
}

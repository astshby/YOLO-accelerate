#ifndef ADAM128_CYCLE_MODEL_H
#define ADAM128_CYCLE_MODEL_H
#include <stdint.h>

// Two reserved output slots; first issue at cycle one, result delivery N+14.
// Independent service-time recurrence for an always-ready convolution sink.
static inline uint64_t adam128_no_stall_cycles(uint64_t pixels, uint64_t beats) {
    uint64_t start=1, previous=0, previous2=0;
    for (uint64_t i=0;i<pixels;++i) {
        if (i) start+=beats;
        if (i>=2 && start<previous2) start=previous2;
        uint64_t delivered=start+beats+13;
        if (delivered<=previous) delivered=previous+1;
        previous2=previous; previous=delivered;
    }
    return previous;
}
#endif

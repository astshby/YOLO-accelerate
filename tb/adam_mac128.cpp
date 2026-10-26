#include "Vadam_mac128.h"
#include "verilated.h"
#include <array>
#include <cstdint>
#include <iostream>
#include <random>
#include <stdexcept>

// Independent scalar dot products, with bubbles and reset at every pipeline phase.
int main() {
    try {
        VerilatedContext context;
        context.threads(1);
        Vadam_mac128 d{&context};
        struct Expected { bool valid=false, last=false; std::array<uint32_t,16> dot{}; };
        std::array<Expected,7> pipe{};
        std::mt19937 rng{0x1282026};
        unsigned checked=0, resets=0;
        for (unsigned cycle=0; cycle<10020; ++cycle) {
            d.rst = cycle == 0 || (cycle < 10000 && cycle % 311 == 0);
            d.in_valid = cycle < 10000 && rng()%4 != 0;
            d.in_last = rng()%5 == 0; // Deliberately set last on invalid inputs too.
            std::array<int8_t,8> activation{};
            d.in_activation=0;
            for (unsigned ic=0; ic<8; ++ic) {
                activation[ic] = cycle%17 == 0 ? -128 : int8_t(rng());
                d.in_activation |= uint64_t(uint8_t(activation[ic])) << (ic*8);
            }
            for (unsigned word=0; word<32; ++word) d.in_weight[word]=0;
            Expected next;
            next.valid=d.in_valid; next.last=d.in_valid && d.in_last;
            for (unsigned oc=0; oc<16; ++oc) {
                int32_t sum=0;
                for (unsigned ic=0; ic<8; ++ic) {
                    const int8_t w = cycle%17 == 0 ? -128 : int8_t(rng());
                    const unsigned byte=oc*8+ic;
                    d.in_weight[byte/4] |= uint32_t(uint8_t(w)) << (8*(byte%4));
                    sum += int(w)*int(activation[ic]);
                }
                next.dot[oc]=uint32_t(sum);
            }
            if (d.rst) { pipe={}; ++resets; }
            else {
                for (unsigned i=6; i>0; --i) pipe[i]=pipe[i-1];
                pipe[0]=next;
            }
            d.clk=0; d.eval(); d.clk=1; d.eval();
            if (bool(d.out_valid)!=pipe[6].valid || bool(d.out_last)!=pipe[6].last)
                throw std::runtime_error("seven-edge valid/last alignment or reset flush");
            if (d.out_valid) {
                for (unsigned oc=0; oc<16; ++oc)
                    if (d.out_dot[oc]!=pipe[6].dot[oc])
                        throw std::runtime_error("pipelined dot product mismatch");
                ++checked;
            }
        }
        std::cout << "PASS adam_mac128: " << checked << " vectors, " << resets
                  << " resets; seven-edge latency, II=1, invalid last, bubbles, INT8 extremes\n";
        d.final();
    } catch (const std::exception& e) {
        std::cerr << "FAIL " << e.what() << '\n'; return 1;
    }
}

#include "Vnpu_conv128.h"
#include "verilated.h"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

static void need(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
int main(int argc, char** argv) {
    try {
        need(argc==2,"workload file required");
        VerilatedContext context; context.threads(1);
        Vnpu_conv128 d{&context};
        auto eval=[&](){ d.clk=0; d.eval(); };
        auto tick=[&](){ eval(); d.clk=1; d.eval(); };
        d.rst=1; d.out_ready=1; tick(); tick(); d.rst=0;
        // Initialize once. Timed commands reuse zeros; transfer cost is reported separately.
        for (unsigned i=0;i<24576;++i) {
            d.act_wr_valid=1; d.act_wr_addr=i; d.act_wr_data=0;
            eval(); need(d.act_wr_ready,"activation preload"); tick();
        }
        d.act_wr_valid=0;
        for (unsigned j=0;j<32;++j) d.weight_wr_data[j]=0;
        for (unsigned i=0;i<864;++i) {
            d.weight_wr_valid=1; d.weight_wr_addr=i;
            eval(); need(d.weight_wr_ready,"weight preload"); tick();
        }
        d.weight_wr_valid=0;
        for (unsigned i=0;i<256;++i) {
            d.cfg_valid=1; d.cfg_kind=2; d.cfg_addr=i; d.cfg_data=i;
            eval(); need(d.cfg_ready,"LUT preload"); tick();
        }
        d.cfg_valid=0;
        std::ifstream input(argv[1]); need(bool(input),"open workloads");
        unsigned id,w,h,ci,co,k,s,p,mode;
        while (input>>id>>w>>h>>ci>>co>>k>>s>>p>>mode) {
            d.cmd_width=w; d.cmd_height=h; d.cmd_cin=ci; d.cmd_cout=co;
            d.cmd_kernel3=k==3; d.cmd_stride2=s==2; d.cmd_pad=p; d.cmd_mode=mode;
            for (unsigned oc=0;oc<16;++oc) d.cmd_bias[oc]=0;
            d.cmd_valid=1; eval(); need(d.cmd_ready,"profile command ready"); tick(); d.cmd_valid=0;
            need(d.busy && !d.cmd_error,"profile descriptor");
            const unsigned ow=(w+2*p-k)/s+1, oh=(h+2*p-k)/s+1, pixels=ow*oh;
            const unsigned steps=pixels*((ci+7)/8)*k*k;
            unsigned outputs=0, elapsed=0;
            while (!d.done && elapsed<steps+100*pixels+100) {
                eval();
                if (d.out_valid) {
                    need(d.out_x==outputs%ow && d.out_y==outputs/ow,"profile output order");
                    need(d.out_mask==((1u<<co)-1) && bool(d.out_last)==(outputs+1==pixels),"profile metadata");
                    for (unsigned oc=0;oc<16;++oc) need(d.out_raw[oc]==0,"profile zero convolution");
                    for (unsigned word=0;word<4;++word) need(d.out_int8[word]==0,"profile zero postprocess");
                    ++outputs;
                }
                tick(); ++elapsed;
            }
            need(d.done && outputs==pixels && !d.busy,"profile completion");
            need(d.perf_mac_steps==steps && d.perf_total_cycles==elapsed,"profile counters");
            std::cout<<id<<' '<<d.perf_conv_cycles<<' '<<d.perf_total_cycles<<' '
                     <<d.perf_post_stalls<<' '<<steps<<' '<<outputs<<'\n';
            tick(); need(!d.done && !d.out_valid,"stale profile command");
        }
        d.final();
    } catch (const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}

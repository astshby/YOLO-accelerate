#include "Vadam_conv128.h"
#include "verilated.h"
#include "../software/adam128_cycle_model.h"
#include <array>
#include <cstdint>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>

static void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

struct Case {
    int w, h, ci, co, k, stride, pad;
    std::vector<int8_t> image, weight;
    std::vector<uint32_t> bias;
    int ow() const { return (w + 2*pad - k) / stride + 1; }
    int oh() const { return (h + 2*pad - k) / stride + 1; }
    int groups() const { return (ci + 7) / 8; }
    int wi(int oc, int ic, int ky, int kx) const { return ((oc*ci + ic)*k + ky)*k + kx; }
};

class Bench {
public:
    static VerilatedContext* single_thread(VerilatedContext& c) { c.threads(1); return &c; }
    VerilatedContext context;
    Vadam_conv128 d;
    std::mt19937 random{0x20261023};
    uint64_t cycles = 0, outputs = 0, convolutions = 0, resets = 0;
    Bench() : d{single_thread(context)} { clear_inputs(); reset(); }
    void clear_inputs() {
        d.act_wr_bank = 0; d.cmd_act_bank = 0;
        d.cmd_valid = 0; d.act_wr_valid = 0; d.weight_wr_valid = 0; d.out_ready = 0;
    }
    void eval() { d.clk = 0; d.eval(); }
    void tick() { eval(); d.clk = 1; d.eval(); ++cycles; }
    void reset() {
        clear_inputs(); d.rst = 1; eval();
        require(!d.cmd_ready && !d.act_wr_ready && !d.weight_wr_ready && !d.out_valid,
                "reset must block all handshakes");
        tick(); tick(); d.rst = 0; eval(); ++resets;
        require(!d.busy && d.cmd_ready, "reset recovery");
    }
    Case make_case(int w, int h, int ci, int co, int k, int stride, int pad, int pattern=0) {
        Case t{w,h,ci,co,k,stride,pad,{},{},{}};
        t.image.resize(w*h*ci); t.weight.resize(co*ci*k*k);
        t.bias.resize(co > 16 ? co : 16);
        for (auto& value : t.image)
            value = pattern == 1 ? -128 : pattern == 2 ? 127 : pattern == 3 ? 0 : int8_t(random() & 255);
        for (auto& value : t.weight)
            value = pattern == 1 ? -128 : pattern == 2 ? -128 : pattern == 3 ? 0 : int8_t(random() & 255);
        for (unsigned oc = 0; oc < t.bias.size(); ++oc)
            t.bias[oc] = pattern == 4 ? (oc & 1 ? 0x80000000u : 0x7fffffffu) : uint32_t(int(random()%20001)-10000);
        return t;
    }
    void load(const Case& t, unsigned bank=0) {
        clear_inputs(); require(!d.busy, "load while busy");
        d.act_wr_bank=bank;
        for (int pixel = 0; pixel < t.w*t.h; ++pixel) {
            for (int group = 0; group < t.groups(); ++group) {
                d.act_wr_data = 0;
                for (int lane = 0; lane < 8; ++lane) {
                    int ic = group*8 + lane;
                    uint8_t v = ic < t.ci ? uint8_t(t.image[pixel*t.ci+ic]) : uint8_t(0x5a);
                    d.act_wr_data |= uint64_t(v) << (lane*8);
                }
                if ((random() & 7) == 0) { d.act_wr_valid = 0; tick(); }
                d.act_wr_addr = pixel*t.groups() + group;
                d.act_wr_valid = 1;
                d.cmd_valid = 1; // A simultaneous command must wait for the write.
                eval(); require(d.act_wr_ready && !d.cmd_ready, "write priority");
                tick(); d.cmd_valid = 0;
            }
        }
        d.act_wr_valid = 0;
        for (int ky = 0; ky < t.k; ++ky) for (int kx = 0; kx < t.k; ++kx) {
            for (int group = 0; group < t.groups(); ++group) {
                for (int word = 0; word < 32; ++word) d.weight_wr_data[word] = 0;
                for (int oc = 0; oc < 16; ++oc) for (int lane = 0; lane < 8; ++lane) {
                    int ic = group*8 + lane, byte = oc*8 + lane;
                    uint8_t v = oc < t.co && ic < t.ci ? uint8_t(t.weight[t.wi(oc,ic,ky,kx)]) : uint8_t(0xa7);
                    d.weight_wr_data[byte/4] |= uint32_t(v) << ((byte%4)*8);
                }
                if ((random() & 7) == 0) { d.weight_wr_valid = 0; tick(); }
                d.weight_wr_addr = (ky*t.k+kx)*t.groups()+group;
                d.weight_wr_valid = 1; eval(); require(d.weight_wr_ready, "weight write ready"); tick();
            }
        }
        d.weight_wr_valid = 0;
    }
    void descriptor(const Case& t) {
        d.cmd_width=t.w; d.cmd_height=t.h; d.cmd_cin=t.ci; d.cmd_cout=t.co;
        d.cmd_kernel3=t.k==3; d.cmd_stride2=t.stride==2; d.cmd_pad=t.pad;
        for (int oc=0; oc<16; ++oc) d.cmd_bias[oc]=t.bias[oc];
    }
    void start(const Case& t, unsigned bank=0) {
        clear_inputs(); d.act_wr_bank=bank; d.cmd_act_bank=bank; descriptor(t); d.cmd_valid=1; eval();
        require(d.cmd_ready, "command not accepted"); tick(); d.cmd_valid=0;
        require(d.busy && !d.cmd_error, "valid descriptor rejected");
    }
    std::vector<uint32_t> reference(const Case& t) {
        const int lanes = t.co > 16 ? t.co : 16;
        std::vector<uint32_t> result(t.ow()*t.oh()*lanes);
        // Standard scalar convolution in a different traversal/layout from the RTL.
        for (int oc=0; oc<t.co; ++oc) for (int y=0; y<t.oh(); ++y) for (int x=0; x<t.ow(); ++x) {
            int64_t sum = int64_t(t.bias[oc]);
            for (int ic=0; ic<t.ci; ++ic) for (int ky=0; ky<t.k; ++ky) for (int kx=0; kx<t.k; ++kx) {
                int iy=y*t.stride+ky-t.pad, ix=x*t.stride+kx-t.pad;
                if (iy>=0 && iy<t.h && ix>=0 && ix<t.w)
                    sum += int(t.image[(iy*t.w+ix)*t.ci+ic]) * int(t.weight[t.wi(oc,ic,ky,kx)]);
            }
            result[(y*t.ow()+x)*lanes+oc] = uint32_t(sum); // Defined INT32 wrap, including bias overflow.
        }
        return result;
    }
    void finish(const Case& t, const std::vector<uint32_t>* full_image_reference=nullptr, bool stalls=true) {
        const uint64_t begin_cycles=cycles;
        const auto golden=full_image_reference ? *full_image_reference : reference(t);
        unsigned pixel=0, held_cycles=0, stall_cycles=0;
        std::array<uint32_t,16> held{};
        unsigned held_x=0, held_y=0, held_mask=0, held_last=0;
        bool stalled=false, saw_done=false;
        const unsigned count=t.ow()*t.oh();
        const uint64_t deadline=cycles + uint64_t(count)*(t.groups()*t.k*t.k+64)+200;
        while (!saw_done && cycles<deadline) {
            // Busy-time writes/commands deliberately carry conflicting values.
            d.act_wr_valid=1; d.weight_wr_valid=1; d.cmd_valid=1;
            d.act_wr_data=0; d.cmd_width=0;
            d.out_ready=!stalls || (random()%4)!=0;
            if (stalls && d.out_valid && pixel==0 && held_cycles<128) { d.out_ready=0; ++held_cycles; }
            eval();
            require(d.busy && !d.cmd_ready && !d.act_wr_ready && !d.weight_wr_ready,
                    "busy accepted a writer or command");
            require(!d.cmd_error, "busy command caused error pulse");
            if (stalled) {
                require(d.out_valid && d.out_x==held_x && d.out_y==held_y &&
                        d.out_mask==held_mask && unsigned(d.out_last)==held_last, "stalled metadata changed");
                for (int oc=0;oc<16;++oc) require(d.out_data[oc]==held[oc], "stalled result changed");
            }
            stalled=d.out_valid && !d.out_ready;
            if (stalled) {
                ++stall_cycles;
                for (int oc=0;oc<16;++oc) held[oc]=d.out_data[oc];
                held_x=d.out_x; held_y=d.out_y; held_mask=d.out_mask; held_last=d.out_last;
            }
            if (d.out_valid && d.out_ready) {
                require(pixel<count, "extra output");
                require(d.out_x==pixel%t.ow() && d.out_y==pixel/t.ow(), "output coordinates");
                require(d.out_mask==((1u<<t.co)-1) && bool(d.out_last)==(pixel+1==count), "output mask/last");
                for (int oc=0;oc<16;++oc) {
                    if (d.out_data[oc]!=golden[pixel*16+oc]) {
                        std::cerr << "case="<<convolutions<<" w="<<t.w<<" h="<<t.h<<" ci="<<t.ci
                                  <<" co="<<t.co<<" k="<<t.k<<" pixel="<<pixel<<" oc="<<oc
                                  <<" got="<<int32_t(d.out_data[oc])<<" expected="<<int32_t(golden[pixel*16+oc])<<'\n';
                        throw std::runtime_error("convolution result mismatch");
                    }
                }
                ++pixel; ++outputs;
            }
            tick(); saw_done=d.done;
        }
        clear_inputs(); eval();
        require(saw_done && pixel==count && !d.busy, "completion/timeout");
        require(d.perf_mac_steps==count*t.groups()*t.k*t.k, "incorrect issued MAC beat count");
        require(d.perf_active_cycles==cycles-begin_cycles,"active cycle counter mismatch");
        require(d.perf_active_cycles>=count*t.groups()*t.k*t.k+14,"pipeline shorter than data dependencies");
        if (!stalls) require(d.perf_active_cycles==adam128_no_stall_cycles(count,t.groups()*t.k*t.k),
                            "two-credit service-time recurrence mismatch");
        else require(stall_cycles>=128,"long output stall not exercised");
        for (int i=0;i<9;++i) { tick(); require(!d.done && !d.out_valid, "stale completion/result"); }
        ++convolutions;
    }
    void run(const Case& t, bool stalls=true, unsigned bank=0) { load(t,bank); start(t,bank); finish(t,nullptr,stalls); }
    void tiled_image(int kernel, int stride) {
        // Cross both the 16x16 input-tile and 16-output-channel boundaries.
        // Golden outputs come from the untiled full image, not local tile convolution.
        const auto full=make_case(29,23,17,33,kernel,stride,kernel==3?1:0);
        const auto full_golden=reference(full);
        const int side=(16-kernel)/stride+1;
        for (int y=0;y<full.oh();y+=side) for (int x=0;x<full.ow();x+=side) {
            const int oh=std::min(side,full.oh()-y), ow=std::min(side,full.ow()-x);
            for (int oc0=0;oc0<full.co;oc0+=16) {
                auto tile=make_case((ow-1)*stride+kernel,(oh-1)*stride+kernel,
                                    full.ci,std::min(16,full.co-oc0),kernel,stride,0);
                for (int iy=0;iy<tile.h;++iy) for (int ix=0;ix<tile.w;++ix) for (int ic=0;ic<tile.ci;++ic) {
                    int sy=y*stride-full.pad+iy, sx=x*stride-full.pad+ix;
                    tile.image[(iy*tile.w+ix)*tile.ci+ic]=
                        sy>=0 && sy<full.h && sx>=0 && sx<full.w ? full.image[(sy*full.w+sx)*full.ci+ic] : 0;
                }
                for (int oc=0;oc<tile.co;++oc) {
                    tile.bias[oc]=full.bias[oc0+oc];
                    for (int ic=0;ic<tile.ci;++ic) for (int ky=0;ky<kernel;++ky) for (int kx=0;kx<kernel;++kx)
                        tile.weight[tile.wi(oc,ic,ky,kx)]=full.weight[full.wi(oc0+oc,ic,ky,kx)];
                }
                std::vector<uint32_t> golden(ow*oh*16);
                for (int iy=0;iy<oh;++iy) for (int ix=0;ix<ow;++ix) for (int oc=0;oc<tile.co;++oc)
                    golden[(iy*ow+ix)*16+oc]=full_golden[((y+iy)*full.ow()+x+ix)*full.co+oc0+oc];
                load(tile); start(tile); finish(tile,&golden);
            }
        }
    }
    void invalid_commands() {
        Case valid=make_case(3,3,8,16,3,1,1);
        for (unsigned which=0;which<9;++which) {
            descriptor(valid);
            switch(which) {
                case 0: d.cmd_width=0; break;
                case 1: d.cmd_height=0; break;
                case 2: d.cmd_cin=0; break;
                case 3: d.cmd_cout=0; break;
                case 4: d.cmd_cout=17; break;
                case 5: d.cmd_width=17; break;
                case 6: d.cmd_cin=769; break;
                case 7: d.cmd_kernel3=0; d.cmd_pad=1; break;
                case 8: d.cmd_width=1; d.cmd_pad=0; break;
            }
            d.cmd_valid=1; eval(); require(d.cmd_ready,"invalid descriptor handshake"); tick();
            require(d.cmd_error && !d.busy && !d.out_valid,"invalid descriptor not rejected");
            d.cmd_valid=0; tick(); require(!d.cmd_error,"error did not clear");
        }
        d.act_wr_addr=32767; d.act_wr_valid=1;
        d.weight_wr_addr=1023; d.weight_wr_valid=1; eval();
        require(!d.act_wr_ready && !d.weight_wr_ready, "out-of-range RAM write accepted");
        tick(); clear_inputs();
    }
    void aborts() {
        const auto t=make_case(5,4,17,13,3,1,1);
        load(t);
        for (unsigned delay : {0u,1u,2u,3u,4u,5u,6u,7u,8u,9u,10u,11u,12u,13u,14u,15u,30u,80u}) {
            start(t);
            for (unsigned n=0;n<delay;++n) tick();
            reset();
            for (int n=0;n<20;++n) { tick(); require(!d.out_valid && !d.done && !d.busy,"old transaction survived reset"); }
            // RAM contents intentionally survive reset; a new command must recover.
            start(t); finish(t);
        }
        start(t); for (int n=0;n<200 && !d.out_valid;++n) tick();
        require(d.out_valid,"reset-at-output setup"); reset(); start(t); finish(t);
    }
};

int main(int argc, char** argv) {
    try {
        // Configure before constructing the model to avoid Verilator worker threads.
        Verilated::threadContextp()->threads(1);
        Bench b;
        b.context.commandArgs(argc,argv);
        b.invalid_commands();
        for (int ci : {1,9,17,41,89,97,105,192,768})
            b.run(b.make_case(16,16,ci,16,1,1,0),false);
        for (int k : {1,3}) for (int stride : {1,2}) for (int ci : {1,3,7,8,9,17,24})
            b.run(b.make_case(5,4,ci,(ci%2)?13:16,k,stride,k==3?1:0));
        b.run(b.make_case(1,1,1,1,3,1,1));
        b.run(b.make_case(7,5,31,4,3,2,0));
        b.run(b.make_case(16,16,8,16,1,1,0));
        b.run(b.make_case(16,16,24,16,3,2,1));
        b.run(b.make_case(16,16,768,16,3,1,1));
        b.run(b.make_case(16,16,768,16,3,1,1),true,1); // bank 1, all 24,576 words and highest read address
        // Cross both 8K segment boundaries with partially occupied input groups.
        for (unsigned bank : {0u,1u}) for (int ci : {257,513})
            b.run(b.make_case(16,16,ci,3,1,1,0),true,bank);
        for (int ci : {48,96,192,384,768}) b.run(b.make_case(3,3,ci,16,3,1,1));
        for (int pattern : {1,2,3,4}) b.run(b.make_case(3,3,768,16,3,1,1,pattern));
        for (int k : {1,3}) for (int stride : {1,2}) b.tiled_image(k,stride);
        b.aborts();
        std::cout << "PASS adam_conv128: " << b.convolutions << " convolutions, " << b.outputs
                  << " output pixels, 4 stitched full-image cases, 9 invalid descriptors, 19 abort/recovery cases; cycles="
                  << b.cycles << "; MACs=128; seed=0x20261023\n";
        b.d.final();
    } catch (const std::exception& error) {
        std::cerr << "FAIL " << error.what() << '\n';
        return 1;
    }
}

// SPDX-License-Identifier: Apache-2.0
// Verilator transport for full feature maps. Only adam_conv128 computes outputs.
#include "Vadam_conv128.h"
#include "verilated.h"
#include "../software/adam128_cycle_model.h"
#include <algorithm>
#include <array>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
constexpr uint32_t magic = 0x314d4441; // Little-endian "ADM1".
void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
uint32_t read_u32() {
    std::array<uint8_t, 4> b{};
    std::cin.read(reinterpret_cast<char*>(b.data()), b.size());
    require(bool(std::cin), "truncated request header");
    return uint32_t(b[0]) | uint32_t(b[1]) << 8 | uint32_t(b[2]) << 16 | uint32_t(b[3]) << 24;
}
void write_uint(uint64_t value, int bytes) {
    for (int i = 0; i < bytes; ++i) std::cout.put(char((value >> (8*i)) & 255));
}
void read_bytes(std::vector<int8_t>& data) {
    std::cin.read(reinterpret_cast<char*>(data.data()), std::streamsize(data.size()));
    require(bool(std::cin), "truncated tensor payload");
}

struct Conv {
    uint32_t id;
    int w, h, ci, co, k, stride, pad;
    bool verify;
    std::vector<int8_t> input, weight; // NCHW (N=1), OIHW.
    int ow() const { return (w + 2*pad - k) / stride + 1; }
    int oh() const { return (h + 2*pad - k) / stride + 1; }
    int groups() const { return (ci + 7) / 8; }
    size_t wi(int oc, int ic, int ky, int kx) const {
        return ((size_t(oc)*ci + ic)*k + ky)*k + kx;
    }
    void read() {
        id = read_u32();
        w = int(read_u32()); h = int(read_u32());
        ci = int(read_u32()); co = int(read_u32()); k = int(read_u32());
        stride = int(read_u32()); pad = int(read_u32());
        const auto check = read_u32();
        require(w > 0 && w <= 416 && h > 0 && h <= 416 && ci > 0 && ci <= 768 &&
                co > 0 && co <= 768 && (k == 1 || k == 3) && (stride == 1 || stride == 2) &&
                pad >= 0 && pad <= k/2 && check <= 1 && w + 2*pad >= k && h + 2*pad >= k,
                "unsupported convolution descriptor");
        verify = bool(check);
        const size_t inputs = size_t(w)*h*ci, weights = size_t(co)*ci*k*k;
        const size_t outputs = size_t(ow())*oh()*co;
        require(inputs <= 8*1024*1024 && weights <= 8*1024*1024 && outputs <= 8*1024*1024,
                "tensor exceeds transport memory bound");
        input.resize(inputs); weight.resize(weights);
        read_bytes(input); read_bytes(weight);
    }
};

struct Stats {
    uint64_t cycles = 0, mac_steps = 0, active_cycles = 0, tiles = 0, checked = 0;
};

class Simulator {
    static VerilatedContext* single_thread(VerilatedContext& c) { c.threads(1); return &c; }
    VerilatedContext context;
    Vadam_conv128 d{single_thread(context)};
    uint64_t cycles = 0;
    struct Tile { int x,y,w,h; };
    Tile current{};
    int current_oc=0,current_co=0;
    unsigned current_pixel=0;
    bool tile_active=false;
    uint64_t tile_deadline=0;
    const Conv* convolution=nullptr;
    std::vector<int32_t>* result=nullptr;
    Stats* statistics=nullptr;
    void eval() { d.clk = 0; d.eval(); }
    void tick() {
        eval();
        if (tile_active && d.out_valid) {
            const auto& t=*convolution;
            unsigned pixels=current.w*current.h;
            require(current_pixel<pixels && d.out_x==current_pixel%current.w && d.out_y==current_pixel/current.w &&
                    d.out_mask==((1u<<current_co)-1) && bool(d.out_last)==(current_pixel+1==pixels),
                    "output metadata mismatch during prefetch");
            for(int oc=0;oc<current_co;++oc) {
                size_t offset=((size_t(current_oc+oc)*t.oh()+current.y+d.out_y)*t.ow()+current.x+d.out_x);
                uint32_t bits=d.out_data[oc];
                (*result)[offset]=bits<=INT32_MAX ? int32_t(bits) : int32_t(int64_t(bits)-(1LL<<32));
            }
            for(int oc=current_co;oc<16;++oc) require(d.out_data[oc]==0,"inactive output lane");
            ++current_pixel;
        }
        d.clk=1; d.eval(); ++cycles;
        if (tile_active && d.done) {
            const auto& t=*convolution;
            unsigned pixels=current.w*current.h,steps=pixels*t.groups()*t.k*t.k;
            require(!d.busy && current_pixel==pixels,"prefetched tile completion");
            require(d.perf_mac_steps==steps && d.perf_active_cycles==adam128_no_stall_cycles(pixels,t.groups()*t.k*t.k),
                    "RTL performance counter mismatch during prefetch");
            statistics->mac_steps+=d.perf_mac_steps;
            statistics->active_cycles+=d.perf_active_cycles; ++statistics->tiles;
            tile_active=false;
        }
    }

    void weights(const Conv& t, int oc0, int channels) {
        for (int ky = 0; ky < t.k; ++ky) for (int kx = 0; kx < t.k; ++kx) {
            for (int g = 0; g < t.groups(); ++g) {
                for (int word = 0; word < 32; ++word) d.weight_wr_data[word] = 0;
                for (int oc = 0; oc < channels; ++oc) for (int lane = 0; lane < 8; ++lane) {
                    int ic = 8*g + lane, byte = 8*oc + lane;
                    if (ic < t.ci)
                        d.weight_wr_data[byte/4] |= uint32_t(uint8_t(t.weight[t.wi(oc0+oc, ic, ky, kx)])) << (8*(byte%4));
                }
                d.weight_wr_addr = (ky*t.k+kx)*t.groups()+g;
                d.weight_wr_valid = 1; eval();
                require(d.weight_wr_ready, "weight RAM backpressure while idle"); tick();
            }
        }
        d.weight_wr_valid = 0;
    }

    void image(const Conv& t, const Tile& tile, int bank) {
        int x0=tile.x,y0=tile.y,w=(tile.w-1)*t.stride+t.k,h=(tile.h-1)*t.stride+t.k;
        d.act_wr_bank=bank;
        for (int y = 0; y < h; ++y) for (int x = 0; x < w; ++x) {
            const int sy = y0*t.stride-t.pad+y, sx = x0*t.stride-t.pad+x;
            const bool inside = sy >= 0 && sy < t.h && sx >= 0 && sx < t.w;
            for (int g = 0; g < t.groups(); ++g) {
                d.act_wr_data = 0;
                if (inside) for (int lane = 0; lane < 8; ++lane) {
                    const int ic = 8*g+lane;
                    if (ic < t.ci)
                        d.act_wr_data |= uint64_t(uint8_t(t.input[(ic*t.h+sy)*t.w+sx])) << (8*lane);
                }
                d.act_wr_addr = (y*w+x)*t.groups()+g;
                d.act_wr_valid = 1; eval();
                require(d.act_wr_ready, "inactive activation bank backpressure"); tick();
            }
        }
        d.act_wr_valid = 0;
    }

    void start_tile(const Conv& t, const Tile& tile, int oc0, int co,int bank) {
        require(!tile_active,"tile already active");
        const int w = (tile.w-1)*t.stride+t.k, h = (tile.h-1)*t.stride+t.k;
        d.cmd_act_bank=bank;
        d.cmd_width = w; d.cmd_height = h; d.cmd_cin = t.ci; d.cmd_cout = co;
        d.cmd_kernel3 = t.k == 3; d.cmd_stride2 = t.stride == 2; d.cmd_pad = 0;
        for (int oc = 0; oc < 16; ++oc) d.cmd_bias[oc] = 0;
        d.cmd_valid = 1; eval(); require(d.cmd_ready, "command not ready"); tick();
        d.cmd_valid = 0;
        require(d.busy && !d.cmd_error, "valid tile rejected");
        const unsigned pixels=tile.w*tile.h,steps=pixels*t.groups()*t.k*t.k;
        tile_deadline=cycles+steps+17*pixels+16;
        current=tile; current_oc=oc0; current_co=co; current_pixel=0; tile_active=true;
    }

    void finish_tile() {
        while (tile_active && cycles<tile_deadline) tick();
        require(!tile_active,"prefetched tile completion timeout");
    }

    static void check(const Conv& t, const std::vector<int32_t>& output) {
        // Independent, untiled scalar INT64 convolution; never used to form the response.
        for (int oc = 0; oc < t.co; ++oc) for (int y = 0; y < t.oh(); ++y) for (int x = 0; x < t.ow(); ++x) {
            int64_t sum = 0;
            for (int ic = 0; ic < t.ci; ++ic) for (int ky = 0; ky < t.k; ++ky) for (int kx = 0; kx < t.k; ++kx) {
                const int sy = y*t.stride+ky-t.pad, sx = x*t.stride+kx-t.pad;
                if (sy >= 0 && sy < t.h && sx >= 0 && sx < t.w)
                    sum += int(t.input[(ic*t.h+sy)*t.w+sx])*int(t.weight[t.wi(oc,ic,ky,kx)]);
            }
            require(sum == output[(oc*t.oh()+y)*t.ow()+x], "RTL/scalar INT64 convolution mismatch");
        }
    }

public:
    Simulator() {
        d.act_wr_bank = 0; d.cmd_act_bank = 0;
        d.cmd_valid = 0; d.act_wr_valid = 0; d.weight_wr_valid = 0; d.out_ready = 1;
        d.rst = 1; tick(); tick(); d.rst = 0; eval();
        require(d.cmd_ready, "reset recovery failed");
    }
    void run(const Conv& t) {
        const uint64_t start = cycles;
        Stats stats;
        std::vector<int32_t> output(size_t(t.ow())*t.oh()*t.co);
        convolution=&t; result=&output; statistics=&stats;
        const int side = (16-t.k)/t.stride+1;
        std::vector<Tile> tiles;
        for(int y=0;y<t.oh();y+=side) for(int x=0;x<t.ow();x+=side)
            tiles.push_back({x,y,std::min(side,t.ow()-x),std::min(side,t.oh()-y)});
        for (int oc = 0; oc < t.co; oc += 16) {
            const int co = std::min(16, t.co-oc);
            weights(t, oc, co); // Weight RAM persists across all spatial tiles of this block.
            image(t,tiles[0],0);
            for(size_t i=0;i<tiles.size();++i) {
                start_tile(t,tiles[i],oc,co,int(i%2));
                if(i+1<tiles.size()) image(t,tiles[i+1],int((i+1)%2));
                finish_tile();
            }
        }
        stats.cycles = cycles-start; // Includes RAM writes and command acceptance.
        if (t.verify) { check(t, output); stats.checked = output.size(); }
        write_uint(magic, 4); write_uint(t.id, 4); write_uint(output.size(), 8);
        write_uint(stats.cycles, 8); write_uint(stats.mac_steps, 8);
        write_uint(stats.active_cycles, 8); write_uint(stats.tiles, 8); write_uint(stats.checked, 8);
        for (int32_t value : output) write_uint(uint32_t(value), 4);
        std::cout.flush(); require(bool(std::cout), "response pipe closed");
    }
};
} // namespace

int main() {
    try {
        std::ios::sync_with_stdio(false);
        std::cin.tie(nullptr);
        Simulator simulator;
        while (std::cin.peek() != std::char_traits<char>::eof()) {
            require(read_u32() == magic, "invalid protocol magic");
            Conv conv; conv.read(); simulator.run(conv);
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "adam_image_server: " << error.what() << '\n';
        return 1;
    }
}

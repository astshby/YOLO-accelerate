#include "Vnpu_conv128.h"
#include "verilated.h"
#include "npu_reference.h"
#include <iostream>
#include <random>
#include <vector>

struct Conv {
    int w,h,ci,co,k,stride,pad;
    std::vector<int8_t> image, weight;
    std::array<uint32_t,16> bias{}, multiplier{};
    std::array<unsigned,16> shift{};
    int ow() const { return (w+2*pad-k)/stride+1; }
    int oh() const { return (h+2*pad-k)/stride+1; }
    int groups() const { return (ci+7)/8; }
    int wi(int oc,int ic,int ky,int kx) const { return ((oc*ci+ic)*k+ky)*k+kx; }
};

class Bench {
    static VerilatedContext* single_thread(VerilatedContext& c) { c.threads(1); return &c; }
public:
    VerilatedContext context;
    Vnpu_conv128 d{single_thread(context)};
    std::mt19937 random{0x20261023};
    std::array<uint8_t,256> table=silu_table();
    uint64_t cycles=0, commands=0, pixels=0, errors=0, aborts=0, post_stalls=0;
    Bench() { d.act_wr_bank=0; d.cmd_act_bank=0; reset(); }
    void eval() { d.clk=0; d.eval(); }
    void tick() { eval(); d.clk=1; d.eval(); ++cycles; }
    void clear() { d.act_wr_valid=0; d.weight_wr_valid=0; d.cmd_valid=0; d.cfg_valid=0; d.out_ready=0; }
    void reset() {
        clear(); d.rst=1; eval();
        require(!d.cmd_ready && !d.cfg_ready && !d.act_wr_ready && !d.weight_wr_ready && !d.out_valid,
                "reset handshakes");
        tick(); tick(); d.rst=0; eval(); require(!d.busy && d.cmd_ready && !d.lut_ready,"reset state");
    }
    void cfg(unsigned kind,unsigned addr,uint32_t data) {
        d.cfg_valid=1; d.cfg_kind=kind; d.cfg_addr=addr; d.cfg_data=data;
        d.cmd_valid=1; eval(); require(d.cfg_ready && !d.cmd_ready,"config command priority");
        tick(); require(!d.cfg_error,"valid configuration rejected");
        d.cfg_valid=0; d.cmd_valid=0;
    }
    void lut() { for (unsigned i=0;i<256;++i) cfg(2,i,table[i]); require(d.lut_ready,"LUT loading"); }
    Conv make(int w,int h,int ci,int co,int k,int s,int p) {
        Conv t{w,h,ci,co,k,s,p,{},{}};
        t.image.resize(w*h*ci); t.weight.resize(co*ci*k*k);
        for (auto& x:t.image) x=int8_t(random()%31)-15;
        for (auto& x:t.weight) x=int8_t(random()%31)-15;
        for (int oc=0;oc<16;++oc) {
            t.bias[oc]=uint32_t(int(random()%2001)-1000);
            t.multiplier[oc]=uint32_t(oc+1)*12347;
            t.shift[oc]=19+(oc%5);
        }
        return t;
    }
    void load(const Conv& t) {
        for (int oc=0;oc<16;++oc) { cfg(0,oc,t.multiplier[oc]); cfg(1,oc,t.shift[oc]); }
        for (int pixel=0;pixel<t.w*t.h;++pixel) for (int g=0;g<t.groups();++g) {
            d.act_wr_data=0;
            for (int lane=0;lane<8;++lane) {
                int ic=g*8+lane;
                uint8_t x=ic<t.ci ? uint8_t(t.image[pixel*t.ci+ic]) : 0xa7;
                d.act_wr_data|=uint64_t(x)<<(8*lane);
            }
            d.act_wr_addr=pixel*t.groups()+g; d.act_wr_valid=1; eval();
            require(d.act_wr_ready,"activation write"); tick();
        }
        d.act_wr_valid=0;
        for (int ky=0;ky<t.k;++ky) for (int kx=0;kx<t.k;++kx) for (int g=0;g<t.groups();++g) {
            for (unsigned word=0;word<32;++word) d.weight_wr_data[word]=0;
            for (int oc=0;oc<16;++oc) for (int lane=0;lane<8;++lane) {
                int ic=g*8+lane,byte=oc*8+lane;
                uint8_t v=oc<t.co && ic<t.ci ? uint8_t(t.weight[t.wi(oc,ic,ky,kx)]) : 0x5a;
                d.weight_wr_data[byte/4]|=uint32_t(v)<<(8*(byte%4));
            }
            d.weight_wr_addr=(ky*t.k+kx)*t.groups()+g; d.weight_wr_valid=1; eval();
            require(d.weight_wr_ready,"weight write"); tick();
        }
        d.weight_wr_valid=0;
    }
    void descriptor(const Conv& t,unsigned mode) {
        d.cmd_mode=mode; d.cmd_width=t.w; d.cmd_height=t.h; d.cmd_cin=t.ci; d.cmd_cout=t.co;
        d.cmd_kernel3=t.k==3; d.cmd_stride2=t.stride==2; d.cmd_pad=t.pad;
        for (int oc=0;oc<16;++oc) d.cmd_bias[oc]=t.bias[oc];
    }
    void start(const Conv& t,unsigned mode) {
        clear(); descriptor(t,mode); d.cmd_valid=1; eval(); require(d.cmd_ready,"command ready");
        tick(); d.cmd_valid=0; require(d.busy && !d.cmd_error,"valid command rejected");
    }
    uint32_t raw(const Conv& t,unsigned pixel,int oc) {
        if (oc>=t.co) return 0;
        int y=pixel/t.ow(),x=pixel%t.ow();
        int64_t sum=t.bias[oc];
        for (int ic=0;ic<t.ci;++ic) for (int ky=0;ky<t.k;++ky) for (int kx=0;kx<t.k;++kx) {
            int iy=y*t.stride+ky-t.pad,ix=x*t.stride+kx-t.pad;
            if (iy>=0 && iy<t.h && ix>=0 && ix<t.w)
                sum+=int(t.image[(iy*t.w+ix)*t.ci+ic])*int(t.weight[t.wi(oc,ic,ky,kx)]);
        }
        return uint32_t(sum);
    }
    void run(const Conv& t,unsigned mode) {
        load(t); start(t,mode);
        const uint64_t begin=cycles;
        const uint64_t deadline=cycles+uint64_t(t.ow()*t.oh())*(t.groups()*t.k*t.k+80)+100;
        unsigned pixel=0,forced_stalls=0,observed_post_stalls=0;
        bool finished=false,held=false;
        std::array<uint32_t,4> previous{};
        while (!finished && cycles<deadline) {
            d.cfg_valid=1; d.cfg_data=0; d.cmd_valid=1; d.cmd_mode=3;
            d.act_wr_valid=1; d.act_wr_data=0; d.weight_wr_valid=1;
            d.out_ready=random()%4!=0;
            if (d.out_valid && forced_stalls<12) { d.out_ready=0; ++forced_stalls; }
            eval();
            require(d.busy && !d.cfg_ready && !d.cmd_ready && !d.act_wr_ready && !d.weight_wr_ready,
                    "operation or configuration accepted while active");
            require(!d.done && !d.cmd_error && !d.cfg_error,"premature completion or unexpected error");
            if (held) {
                require(d.out_valid,"stalled valid dropped");
                for (unsigned word=0;word<4;++word) require(d.out_int8[word]==previous[word],"stalled data changed");
            }
            held=d.out_valid && !d.out_ready;
            if (d.out_valid) {
                require(d.out_x==pixel%t.ow() && d.out_y==pixel/t.ow() &&
                        d.out_mask==((1u<<t.co)-1) && d.out_mode==mode &&
                        bool(d.out_last)==(pixel+1==unsigned(t.ow()*t.oh())),"metadata mismatch");
                for (int oc=0;oc<16;++oc) {
                    auto expected=raw(t,pixel,oc);
                    require(d.out_raw[oc]==expected,"raw convolution/bias mismatch");
                    const uint8_t q=uint8_t(requant_reference(signed32(expected),t.multiplier[oc],t.shift[oc]));
                    const uint8_t expected_q=mode==0 || oc>=t.co ? 0 : mode==2 ? table[q] : q;
                    require(uint8_t(d.out_int8[oc/4]>>(8*(oc%4)))==expected_q,"integrated INT8 mismatch");
                }
                for (unsigned word=0;word<4;++word) previous[word]=d.out_int8[word];
                if (d.out_ready) ++pixel;
            }
            observed_post_stalls=d.perf_post_stalls;
            tick(); finished=d.done;
        }
        clear(); eval();
        require(finished && pixel==unsigned(t.ow()*t.oh()) && !d.busy,"completion timeout");
        require(d.perf_total_cycles==cycles-begin,"total cycle counter");
        require(d.perf_mac_steps==unsigned(t.ow()*t.oh()*t.groups()*t.k*t.k),"MAC counter");
        require(d.perf_total_cycles>=d.perf_conv_cycles && d.perf_post_stalls>=observed_post_stalls,
                "pipeline counters");
        for (int i=0;i<3;++i) { tick(); require(!d.done && !d.out_valid,"completion must pulse"); }
        ++commands; pixels+=pixel; post_stalls+=d.perf_post_stalls;
    }
    void invalid(const Conv& t,unsigned mode,bool zero_width=false) {
        clear(); descriptor(t,mode); if (zero_width) d.cmd_width=0;
        d.cmd_valid=1; eval(); require(d.cmd_ready,"invalid command handshake"); tick(); d.cmd_valid=0;
        require(d.cmd_error && !d.out_valid,"invalid command not rejected");
        tick(); require(!d.busy && !d.cmd_error && !d.done,"invalid command recovery"); ++errors;
    }
    void test() {
        auto small=make(5,4,17,13,3,1,1);
        invalid(small,2); invalid(small,3); invalid(small,0,true); lut();
        for (int ci:{769,32768,65535}) { auto bad=small; bad.ci=ci; invalid(bad,0); }
        for (int width:{17,128,255}) { auto bad=small; bad.w=width; invalid(bad,0); }
        for (unsigned mode=0;mode<3;++mode) {
            for (int k:{1,3}) for (int stride:{1,2}) {
                run(make(7,5,17,13,k,stride,k/2),mode);
                run(make(3,3,1,1,k,stride,k/2),mode);
            }
            run(make(3,3,768,16,3,1,1),mode);
            auto wrap=make(1,1,8,16,1,1,0);
            wrap.bias.fill(0x7fffffffu); wrap.image.assign(8,127); wrap.weight.assign(128,127);
            run(wrap,mode);
        }
        for (unsigned delay:{0u,5u,40u}) {
            load(small); start(small,2);
            for (unsigned n=0;n<delay;++n) tick();
            reset();
            for (unsigned n=0;n<20;++n) { tick(); require(!d.out_valid && !d.done,"old output after reset"); }
            invalid(small,2); lut(); run(small,2); ++aborts;
        }
        require(post_stalls>0,"test never exercised postprocess backpressure");
    }
};

int main() {
    try {
        Bench b; b.test();
        std::cout<<"PASS npu_conv128: "<<b.commands<<" tile commands, "<<b.pixels
                 <<" output pixels in three modes, "<<b.errors<<" invalid commands, "
                 <<b.aborts<<" reset recoveries, "<<b.post_stalls<<" postprocess stall cycles\n";
    } catch (const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}

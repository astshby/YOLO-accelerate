#include "Vnpu_conv128.h"
#include "verilated.h"
#include "npu_reference.h"
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

struct Tile {
    int w,h,ci,co,k,s,p,mode,seed;
    int groups() const { return (ci+7)/8; }
    int ow() const { return (w+2*p-k)/s+1; }
    int oh() const { return (h+2*p-k)/s+1; }
    int words() const { return w*h*groups(); }
};

class PrefetchBench {
    static VerilatedContext* single_thread(VerilatedContext& c) { c.threads(1); return &c; }
    VerilatedContext context;
    Vnpu_conv128 d{single_thread(context)};
    Tile current{};
    bool active=false, zero=false, stalls=false, force_hold=false;
    unsigned received=0, held=0;
    uint64_t commands=0, outputs=0, rejected=0, recoveries=0;
    int8_t activation(const Tile& t,int y,int x,int ic) const {
        return zero ? 0 : int8_t((t.seed*11+y*7+x*3+ic*5)%31-15);
    }
    int8_t weight(int oc,int ic,int ky,int kx) const {
        return zero ? 0 : int8_t((oc*3+ic+ky*5+kx*7)%17-8);
    }
    uint32_t bias(const Tile& t,int oc) const { return zero ? 0 : uint32_t(t.seed+oc*5-17); }
    void eval() { d.clk=0; d.eval(); }
    void check_output() {
        const Tile& t=current;
        require(received<unsigned(t.ow()*t.oh()),"prefetch excess output");
        int x=received%t.ow(),y=received/t.ow();
        require(d.out_x==x && d.out_y==y && d.out_mask==((1u<<t.co)-1) &&
                bool(d.out_last)==(received+1==unsigned(t.ow()*t.oh())) && d.out_mode==t.mode,
                "prefetch output metadata");
        for (int oc=0;oc<16;++oc) {
            int64_t sum=oc<t.co ? int32_t(bias(t,oc)) : 0;
            if (!zero && oc<t.co) for (int ic=0;ic<t.ci;++ic)
                for (int ky=0;ky<t.k;++ky) for (int kx=0;kx<t.k;++kx) {
                    int sy=y*t.s+ky-t.p,sx=x*t.s+kx-t.p;
                    if (sy>=0 && sy<t.h && sx>=0 && sx<t.w)
                        sum+=int(activation(t,sy,sx,ic))*int(weight(oc,ic,ky,kx));
                }
            require(d.out_raw[oc]==uint32_t(sum),"prefetch independent INT64 result");
            uint8_t q=t.mode && oc<t.co ? uint8_t(requant_reference(int32_t(sum),19,5)) : 0;
            require(uint8_t(d.out_int8[oc/4]>>(8*(oc%4)))==q,"prefetch quantization/identity LUT");
        }
    }
public:
    uint64_t cycles=0, total_active=0, conv_active=0, mac_steps=0, post_stalls=0;
    uint64_t act_writes=0, weight_writes=0, overlap_writes=0;
    PrefetchBench(bool zeros=false) : zero(zeros) {
        reset(); configure();
    }
    void step() {
        d.out_ready=!force_hold && (!stalls || cycles%5!=0);
        eval();
        if (stalls && active && d.out_valid && !received && held<128) {
            d.out_ready=0; ++held; eval();
        }
        if (active && d.out_valid) {
            check_output();
            if (d.out_ready) { ++received; ++outputs; }
        }
        if (d.act_wr_valid && d.act_wr_ready) {
            ++act_writes; if (d.busy) ++overlap_writes;
        }
        if (d.weight_wr_valid && d.weight_wr_ready) ++weight_writes;
        d.clk=1; d.eval(); ++cycles;
        if (active && d.done) {
            require(!d.busy && received==unsigned(current.ow()*current.oh()),"prefetch completion");
            uint64_t steps=uint64_t(received)*current.groups()*current.k*current.k;
            require(d.perf_mac_steps==steps,"prefetch MAC count");
            total_active+=d.perf_total_cycles; conv_active+=d.perf_conv_cycles;
            mac_steps+=steps; post_stalls+=d.perf_post_stalls;
            active=false;
        }
    }
    void reset() {
        d.act_wr_valid=0; d.weight_wr_valid=0; d.cfg_valid=0; d.cmd_valid=0;
        d.act_wr_bank=0; d.cmd_act_bank=0; force_hold=false; active=false;
        d.rst=1; eval();
        require(!d.act_wr_ready && !d.weight_wr_ready && !d.cmd_ready && !d.cfg_ready,"prefetch reset gating");
        step(); step(); d.rst=0; eval();
        require(!d.busy && d.cmd_ready,"prefetch reset idle");
    }
    void configure() {
        d.cfg_valid=1;
        for (int oc=0;oc<16;++oc) for (int kind=0;kind<2;++kind) {
            d.cfg_kind=kind; d.cfg_addr=oc; d.cfg_data=kind ? 5 : 19;
            eval(); require(d.cfg_ready,"prefetch coefficient config"); step();
        }
        d.cfg_kind=2;
        for (int i=0;i<256;++i) { d.cfg_addr=i; d.cfg_data=i; step(); }
        d.cfg_valid=0; require(d.lut_ready,"prefetch identity LUT");
    }
    void load_weights(const Tile& t) {
        require(!active,"prefetch weights while active");
        d.weight_wr_valid=1;
        for (int ky=0;ky<t.k;++ky) for (int kx=0;kx<t.k;++kx) for (int g=0;g<t.groups();++g) {
            for (int word=0;word<32;++word) d.weight_wr_data[word]=0;
            for (int oc=0;oc<16;++oc) for (int lane=0;lane<8;++lane) {
                int ic=g*8+lane,byte=oc*8+lane;
                uint8_t v=oc<t.co && ic<t.ci ? uint8_t(weight(oc,ic,ky,kx)) : 0xa7;
                d.weight_wr_data[byte/4]|=uint32_t(v)<<(8*(byte%4));
            }
            d.weight_wr_addr=(ky*t.k+kx)*t.groups()+g;
            eval(); require(d.weight_wr_ready,"prefetch weight ready"); step();
        }
        d.weight_wr_valid=0;
    }
    void load_input(const Tile& t,int bank,int first=0,int count=-1) {
        d.act_wr_bank=bank; d.act_wr_valid=1;
        if (count<0) count=t.words()-first;
        for (int word=first;word<first+count;++word) {
            int pixel=word/t.groups(),g=word%t.groups(); d.act_wr_data=0;
            for (int lane=0;lane<8;++lane) {
                int ic=g*8+lane;
                uint8_t v=ic<t.ci ? uint8_t(activation(t,pixel/t.w,pixel%t.w,ic)) : 0x5a;
                d.act_wr_data|=uint64_t(v)<<(lane*8);
            }
            d.act_wr_addr=word; eval(); require(d.act_wr_ready,"inactive bank write blocked"); step();
        }
        d.act_wr_valid=0;
    }
    void launch(const Tile& t,int bank) {
        require(!active,"prefetch launch while active");
        d.cmd_act_bank=bank; d.cmd_width=t.w; d.cmd_height=t.h; d.cmd_cin=t.ci; d.cmd_cout=t.co;
        d.cmd_kernel3=t.k==3; d.cmd_stride2=t.s==2; d.cmd_pad=t.p; d.cmd_mode=t.mode;
        for (int oc=0;oc<16;++oc) d.cmd_bias[oc]=bias(t,oc);
        d.cmd_valid=1; eval(); require(d.cmd_ready,"prefetch command ready"); step(); d.cmd_valid=0;
        require(d.busy && !d.cmd_error,"prefetch descriptor rejected");
        current=t; active=true; received=0; held=0; ++commands;
    }
    void finish() {
        uint64_t deadline=cycles+uint64_t(current.ow()*current.oh())*(current.groups()*current.k*current.k+200)+100;
        while (active && cycles<deadline) step();
        require(!active,"prefetch completion timeout");
    }
    void conflicts(int bank) {
        // Hold the final NPU output too: core-idle/post-busy must still protect the active bank.
        force_hold=true;
        for (int i=0;i<100 && !d.out_valid;++i) step();
        d.act_wr_bank=bank; d.act_wr_addr=0; d.act_wr_data=~uint64_t(0); d.act_wr_valid=1;
        d.weight_wr_valid=1; d.cfg_valid=1; d.cmd_valid=1; d.cmd_act_bank=!bank;
        for (int i=0;i<32;++i) {
            eval(); require(d.busy && !d.act_wr_ready && !d.weight_wr_ready && !d.cfg_ready && !d.cmd_ready,
                            "active-bank/config/weight conflict accepted"); ++rejected; step();
        }
        d.act_wr_valid=0; d.weight_wr_valid=0; d.cfg_valid=0; d.cmd_valid=0; force_hold=false;
    }
    void group(const std::vector<Tile>& tiles,bool overlap,bool inject=false) {
        require(!tiles.empty(),"empty group");
        load_weights(tiles[0]); load_input(tiles[0],0);
        for (size_t i=0;i<tiles.size();++i) {
            launch(tiles[i],int(i%2));
            if (inject) conflicts(int(i%2));
            if (overlap && i+1<tiles.size()) load_input(tiles[i+1],int((i+1)%2));
            finish();
            if (!overlap && i+1<tiles.size()) load_input(tiles[i+1],int((i+1)%2));
        }
    }
    void tests() {
        stalls=true;
        for (int mode=0;mode<3;++mode) for (int k : {1,3}) for (int s : {1,2}) {
            std::vector<Tile> ts;
            for (int i=0;i<5;++i) ts.push_back({5+i%2,4+i%3,17,13,k,s,k/2,mode,i+1});
            group(ts,true,true);
        }
        for (int ci : {1,8,97,768}) {
            Tile a{1,1,ci,16,1,1,0,2,5}; group({a,a,a},true,true);
        }
        // Cancel in-flight compute and a partially loaded other bank, then overwrite and recover.
        for (int delay : {0,5,15,80}) {
            Tile a{7,6,17,16,3,1,1,2,delay+1},b=a; b.seed+=7;
            load_weights(a); load_input(a,0); launch(a,0); force_hold=true;
            load_input(b,1,0,11); for(int n=0;n<delay;++n) step();
            reset(); for(int n=0;n<20;++n) { step(); require(!d.out_valid && !d.done,"stale output after prefetch reset"); }
            configure(); load_input(b,1); launch(b,1); finish(); ++recoveries;
        }
        for (int bank=0;bank<2;++bank) {
            d.act_wr_bank=bank; d.act_wr_addr=32767; d.act_wr_valid=1;
            eval(); require(!d.act_wr_ready,"bank out-of-range address accepted"); step(); d.act_wr_valid=0;
        }
        require(overlap_writes>0 && rejected>0,"prefetch overlap coverage missing");
        std::cout<<"PASS npu_prefetch: "<<commands<<" launches, "<<outputs<<" output vectors, "
                 <<overlap_writes<<" busy-time inactive-bank writes, "<<rejected<<" protected conflict clocks, "
                 <<recoveries<<" partial-prefetch reset recoveries; 128 MAC, 3 modes, both banks\n";
    }
};

int main(int argc,char** argv) {
    try {
        Verilated::threadContextp()->threads(1);
        if (argc==1) { PrefetchBench bench; bench.tests(); return 0; }
        require(argc==2,"workload file argument");
        for (bool overlap : {false,true}) {
            PrefetchBench bench(true);
            std::ifstream input(argv[1]); require(bool(input),"open prefetch workloads");
            std::string op; int size=0,mode=0; uint64_t begin=0;
            while (input>>op) {
                if (op=="R") { input>>size>>mode; begin=bench.cycles; }
                else if (op=="B") {
                    unsigned count; input>>count; std::vector<Tile> ts;
                    for (unsigned i=0;i<count;++i) {
                        Tile t{}; input>>t.w>>t.h>>t.ci>>t.co>>t.k>>t.s>>t.p;
                        t.mode=mode; ts.push_back(t);
                    }
                    bench.group(ts,overlap);
                } else if (op=="E") {
                    std::cout<<"{\"size\":"<<size<<",\"mode\":"<<mode<<",\"overlap\":"<<int(overlap)
                        <<",\"cycles\":"<<bench.cycles-begin<<",\"active_cycles\":"<<bench.total_active
                        <<",\"conv_cycles\":"<<bench.conv_active<<",\"mac_steps\":"<<bench.mac_steps
                        <<",\"post_stalls\":"<<bench.post_stalls<<",\"act_writes\":"<<bench.act_writes
                        <<",\"weight_writes\":"<<bench.weight_writes<<",\"overlap_writes\":"<<bench.overlap_writes<<"}\n";
                    bench.total_active=bench.conv_active=bench.mac_steps=bench.post_stalls=0;
                    bench.act_writes=bench.weight_writes=bench.overlap_writes=0;
                } else require(false,"unknown prefetch workload opcode");
            }
        }
    } catch(const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}

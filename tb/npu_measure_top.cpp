#include "Vnpu_measure_top.h"
#include "verilated.h"
#include "npu_reference.h"
#include <iostream>
#include <cstdlib>
#include <fstream>
#include <vector>

struct Request {
    bool act_bank=false, cmd_bank=false;
    bool act_valid=false, weight_valid=false, cfg_valid=false, cmd_valid=false, ready=false;
    unsigned act_addr=0, weight_addr=0, kind=0, cfg_addr=0, mode=0;
    uint64_t activation=0;
    uint32_t cfg_data=0;
    std::array<uint32_t,32> weight{};
    std::array<uint32_t,16> bias{};
    unsigned w=1,h=1,ci=8,co=13,k3=0,s2=0,pad=0;
    std::vector<bool> bits() const {
        std::vector<bool> b;
        auto push=[&](uint64_t value,unsigned width) {
            for (int i=int(width)-1;i>=0;--i) b.push_back((value>>i)&1);
        };
        push(act_bank,1); push(cmd_bank,1);
        push(act_valid,1); push(act_addr,15); push(activation,64);
        push(weight_valid,1); push(weight_addr,10);
        for (int i=31;i>=0;--i) push(weight[i],32);
        push(cfg_valid,1); push(kind,2); push(cfg_addr,8); push(cfg_data,32);
        push(cmd_valid,1); push(mode,2); push(w,8); push(h,8); push(ci,16); push(co,5);
        push(k3,1); push(s2,1); push(pad,1);
        for (int i=15;i>=0;--i) push(bias[i],32);
        push(ready,1);
        require(b.size()==1716,"scan input width"); return b;
    }
};

class Bench {
    static VerilatedContext* single_thread(VerilatedContext& c) { c.threads(1); return &c; }
public:
    VerilatedContext context;
    Vnpu_measure_top d{single_thread(context)};
    std::ofstream trace;
    unsigned transactions=0;
    Bench() {
        if (const char* path=std::getenv("NPU_PIN_TRACE")) {
            trace.open(path); require(bool(trace),"open pin trace");
        }
        d.scan_shift=0; d.scan_update=0; d.scan_capture=0; d.scan_din=0;
        d.rst=1; tick(); tick(); d.rst=0;
    }
    void half() {
        const auto target=context.time()+5000; // 5 ns at the 1 ps precision of GTP models.
        d.eval();
        while (d.eventsPending() && d.nextTimeSlot()<=target) { context.time(d.nextTimeSlot()); d.eval(); }
        context.time(target); d.eval();
    }
    void tick() {
        d.clk=0; half(); d.clk=1; half();
        if (trace.is_open()) trace<<std::hex<<unsigned((d.rst<<5)|(d.scan_shift<<4)|
            (d.scan_update<<3)|(d.scan_capture<<2)|(d.scan_din<<1)|d.scan_dout)<<'\n';
    }
    void send(const Request& request) {
        d.scan_shift=1;
        for (bool b:request.bits()) { d.scan_din=b; tick(); }
        d.scan_shift=0; d.scan_update=1; tick(); d.scan_update=0; tick();
        ++transactions;
    }
    std::vector<bool> capture() {
        d.scan_capture=1; tick(); d.scan_capture=0; d.scan_shift=1;
        std::vector<bool> b;
        for (int i=0;i<813;++i) { b.push_back(d.scan_dout); tick(); }
        d.scan_shift=0; return b;
    }
    void test() {
        auto initial=capture();
        require(initial[0] && initial[1] && initial[2] && initial[5] && !initial[4] && !initial[7],
                "scan ready/reset snapshot");
        Request req;
        req.act_valid=true;
        for (int i=0;i<8;++i) req.activation|=uint64_t(i+1)<<(8*i);
        send(req); req.act_valid=false; req.weight_valid=true;
        std::array<int32_t,16> raw{};
        for (int oc=0;oc<16;++oc) for (int ic=0;ic<8;++ic) {
            int8_t weight=int8_t((oc*3+ic)%17-8);
            int byte=oc*8+ic;
            req.weight[byte/4]|=uint32_t(uint8_t(weight))<<(8*(byte%4));
            raw[oc]+=(ic+1)*int(weight);
        }
        send(req); req.weight_valid=false; req.cfg_valid=true;
        for (int oc=0;oc<16;++oc) {
            req.kind=0; req.cfg_addr=oc; req.cfg_data=19; send(req);
            req.kind=1; req.cfg_data=5; send(req);
        }
        const auto table=silu_table();
        for (unsigned i=0;i<256;++i) { req.kind=2; req.cfg_addr=i; req.cfg_data=table[i]; send(req); }
        req.cfg_valid=false;
        auto loaded=capture(); require(loaded[4],"scan LUT loading");
        auto execute=[&](unsigned count,unsigned mode,unsigned steps,bool preload=false) {
            req.co=count;
            req.cmd_valid=true; req.mode=mode; send(req); req.cmd_valid=false;
            for (int i=0;i<80;++i) tick();
            if (preload) {
                req.act_bank=true; req.act_valid=true;
                for (int group=0;group<3;++group) {
                    req.act_addr=group; req.activation=0;
                    for (int lane=0;lane<8;++lane) {
                        int ic=group*8+lane;
                        uint8_t v=ic<17 ? uint8_t(int8_t((ic*7+5)%29-14)) : 0xa7;
                        req.activation|=uint64_t(v)<<(lane*8);
                    }
                    send(req);
                }
                req.act_bank=false; req.act_addr=0; req.activation=~uint64_t(0); send(req);
                req.act_valid=false;
                auto protected_bank=capture(); require(!protected_bank[0],"scan active bank write protection");
            }
            const auto output=capture();
            size_t cursor=9;
            auto get=[&](unsigned width) {
                uint64_t value=0;
                for (unsigned i=0;i<width;++i) value=(value<<1)|output[cursor++];
                return value;
            };
            require(output[7] && !output[8] && get(1)==1 && get(2)==mode,"scan output status/mode");
            for (int oc=15;oc>=0;--oc)
                require(uint32_t(get(32))==(unsigned(oc)<count ? uint32_t(raw[oc]) : 0),"scan raw result");
            for (int oc=15;oc>=0;--oc) {
                auto q=uint8_t(requant_reference(raw[oc],19,5));
                uint8_t expected=mode==0 || unsigned(oc)>=count ? 0 : mode==2 ? table[q] : q;
                require(get(8)==expected,"scan INT8 result");
            }
            require(get(16)==((1u<<count)-1) && get(8)==0 && get(8)==0 && get(1)==1,"scan coordinates/mask/last");
            require(get(32)==steps,"scan MAC counter");
            req.ready=true; send(req); req.ready=false;
            const auto completed=capture();
            require(completed[8] && !completed[7] && !completed[9],"scan completion handshake");
        };
        for (unsigned count=1;count<=16;++count) for (unsigned mode=0;mode<3;++mode) execute(count,mode,1);
        // One output from 2x2/Ci=17, 3x3/stride2/pad1: exercise narrow unsigned casts,
        // three input groups, padding and tail-byte masking in the mapped netlist too.
        req.w=2; req.h=2; req.ci=17; req.k3=1; req.s2=1; req.pad=1;
        auto activation=[](int pixel,int ic) { return int8_t((pixel*7+ic*3)%31-15); };
        auto weight=[](int oc,int ic,int ky,int kx) { return int8_t((oc*3+ic+ky*5+kx*7)%17-8); };
        req.act_valid=true;
        for (int pixel=0;pixel<4;++pixel) for (int group=0;group<3;++group) {
            req.act_addr=pixel*3+group; req.activation=0;
            for (int lane=0;lane<8;++lane) {
                int ic=group*8+lane;
                uint8_t value=ic<17 ? uint8_t(activation(pixel,ic)) : 0xa7;
                req.activation|=uint64_t(value)<<(lane*8);
            }
            send(req);
        }
        req.act_valid=false; req.weight_valid=true; raw.fill(0);
        for (int ky=0;ky<3;++ky) for (int kx=0;kx<3;++kx) for (int group=0;group<3;++group) {
            req.weight_addr=(ky*3+kx)*3+group; req.weight.fill(0);
            for (int oc=0;oc<16;++oc) for (int lane=0;lane<8;++lane) {
                int ic=group*8+lane, byte=oc*8+lane;
                uint8_t value=ic<17 ? uint8_t(weight(oc,ic,ky,kx)) : 0x5a;
                req.weight[byte/4]|=uint32_t(value)<<(8*(byte%4));
                if (ic<17 && ky>=1 && kx>=1)
                    raw[oc]+=int(activation((ky-1)*2+kx-1,ic))*int(weight(oc,ic,ky,kx));
            }
            send(req);
        }
        req.weight_valid=false;
        for (unsigned mode=0;mode<3;++mode) execute(16,mode,27);
        // Four distinct pixels held at each output: fill the result credits,
        // pause the producer, then resume with one-cycle ready pulses.
        req.k3=0; req.s2=0; req.pad=0; req.co=16;
        auto point_weight=[](int oc,int ic) { return int8_t((oc*5+ic)%15-7); };
        req.weight_valid=true;
        for (int group=0;group<3;++group) {
            req.weight_addr=group; req.weight.fill(0);
            for (int oc=0;oc<16;++oc) for (int lane=0;lane<8;++lane) {
                int ic=group*8+lane,byte=oc*8+lane;
                uint8_t w=ic<17 ? uint8_t(point_weight(oc,ic)) : 0xa7;
                req.weight[byte/4]|=uint32_t(w)<<(8*(byte%4));
            }
            send(req);
        }
        req.weight_valid=false;
        for (int oc=0;oc<16;++oc) req.bias[oc]=uint32_t(oc*5-17);
        for (unsigned mode=0;mode<3;++mode) {
            req.mode=mode; req.cmd_valid=true; send(req); req.cmd_valid=false;
            for (unsigned pixel=0;pixel<4;++pixel) {
                for (int i=0;i<80;++i) tick();
                auto output=capture(); size_t cursor=9;
                auto get=[&](unsigned width) { uint64_t v=0; for(unsigned i=0;i<width;++i) v=(v<<1)|output[cursor++]; return v; };
                require(output[7] && !output[8] && get(1)==1 && get(2)==mode,"multi-pixel status");
                std::array<int32_t,16> expected{};
                for (int oc=0;oc<16;++oc) {
                    expected[oc]=int32_t(req.bias[oc]);
                    for (int ic=0;ic<17;++ic) expected[oc]+=int(activation(pixel,ic))*int(point_weight(oc,ic));
                }
                for (int oc=15;oc>=0;--oc) require(uint32_t(get(32))==uint32_t(expected[oc]),"multi-pixel bias/dot");
                for (int oc=15;oc>=0;--oc) {
                    uint8_t q=uint8_t(requant_reference(expected[oc],19,5));
                    require(get(8)==(mode==0 ? 0 : mode==2 ? table[q] : q),"multi-pixel quantization");
                }
                require(get(16)==0xffff && get(8)==pixel%2 && get(8)==pixel/2 && get(1)==(pixel==3),"multi-pixel order");
                req.ready=true; send(req); req.ready=false;
            }
            auto completed=capture(); require(completed[8] && !completed[7] && !completed[9],"multi-pixel completion");
        }
        // Preload bank 1 during bank 0's stalled output, then consume bank 1.
        // Re-run bank 0 afterwards to prove the rejected poison write did not land.
        req.w=1; req.h=1;
        auto bank_reference=[&](bool bank) {
            for(int oc=0;oc<16;++oc) {
                raw[oc]=int32_t(req.bias[oc]);
                for(int ic=0;ic<17;++ic) {
                    int8_t a=bank ? int8_t((ic*7+5)%29-14) : activation(0,ic);
                    raw[oc]+=int(a)*int(point_weight(oc,ic));
                }
            }
        };
        for(unsigned mode=0;mode<3;++mode) {
            req.cmd_bank=false; bank_reference(false); execute(16,mode,3,true);
            req.cmd_bank=true; bank_reference(true); execute(16,mode,3);
            req.cmd_bank=false; bank_reference(false); execute(16,mode,3);
        }
        req.cmd_valid=true; req.mode=3; send(req);
        const auto invalid=capture(); require(invalid[6] && !invalid[7],"scan invalid command status");
    }
};

int main() {
    try {
        Bench b; b.test();
        std::cout<<"PASS npu_measure_top: "<<b.transactions
                 <<" scan transactions, 1716 input/813 output bits, all 16 channel counts, three modes, 3x3/stride2/pad1/Ci17, four-pixel credit stalls, concurrent inactive-bank preloads/protected poison writes/bank switches and invalid command\n";
    } catch (const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}

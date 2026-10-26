#include "Vnpu_mmio128.h"
#include "verilated.h"
#include "npu_reference.h"
#include "../software/npu128_mmio.h"
#include <deque>
#include <iostream>
#include <map>
#include <random>
#include <vector>

struct Task {
    unsigned w,h,ci,co,k,s,p;
    std::vector<int8_t> image,weight;
    std::array<uint32_t,16> bias{},multiplier{};
    std::array<unsigned,16> shift{};
    unsigned groups() const { return (ci+7)/8; }
    unsigned ow() const { return (w+2*p-k)/s+1; }
    unsigned oh() const { return (h+2*p-k)/s+1; }
    unsigned wi(unsigned oc,unsigned ic,unsigned ky,unsigned kx) const {
        return ((oc*ci+ic)*k+ky)*k+kx;
    }
    uint32_t reference(unsigned pixel,unsigned oc) const {
        if (oc>=co) return 0;
        int64_t sum=bias[oc];
        for (unsigned ic=0;ic<ci;ic++) for (unsigned ky=0;ky<k;ky++) for (unsigned kx=0;kx<k;kx++) {
            int y=int(pixel/ow()*s+ky)-int(p),x=int(pixel%ow()*s+kx)-int(p);
            if (x>=0 && y>=0 && x<int(w) && y<int(h))
                sum+=int(image[(unsigned(y)*w+unsigned(x))*ci+ic])*int(weight[wi(oc,ic,ky,kx)]);
        }
        return uint32_t(sum);
    }
};

class Bench {
    static VerilatedContext* single_thread(VerilatedContext& c) { c.threads(1); return &c; }
public:
    VerilatedContext context;
    Vnpu_mmio128 d{single_thread(context)};
    std::mt19937 random{0x128c5a};
    uint64_t cycles=0,transactions=0,response_stalls=0,access_errors=0;
    unsigned tasks=0,pixels=0,aborts=0;
    int fail_read=-1;
    npu128_io io{this,read_callback,write_callback};
    std::array<uint8_t,256> table{};
    Bench() {
        for (unsigned i=0;i<256;i++) table[i]=uint8_t(i^0xa5);
        d.req_valid=0; d.rsp_ready=0; d.req_write=0; d.req_addr=0; d.req_wdata=0; d.req_wstrb=0;
        hard_reset();
    }
    static int read_callback(void* self,uint32_t address,uint32_t* value) {
        auto& b=*static_cast<Bench*>(self);
        if (b.fail_read==int(address)) { b.fail_read=-1; return -9; }
        auto result=b.transfer(false,address,0,0);
        *value=result.first; return result.second ? -1 : 0;
    }
    static int write_callback(void* self,uint32_t address,uint32_t value,uint8_t strobes) {
        return static_cast<Bench*>(self)->transfer(true,address,value,strobes).second ? -1 : 0;
    }
    void eval() { d.clk=0; d.eval(); }
    void tick() { eval(); d.clk=1; d.eval(); ++cycles; }
    void idle(unsigned count) { d.req_valid=0; d.rsp_ready=0; for (unsigned i=0;i<count;i++) tick(); }
    void hard_reset() {
        d.rst=1; eval(); require(!d.req_ready,"request ready during reset"); tick(); tick();
        require(!d.rsp_valid,"external reset must cancel response");
        d.req_valid=0; d.rsp_ready=0; d.rst=0; eval();
    }
    std::pair<uint32_t,bool> transfer(bool write,uint32_t address,uint32_t value,uint8_t strobes) {
        require(address<=65535,"TB address width");
        d.req_valid=1; d.req_write=write; d.req_addr=address; d.req_wdata=value; d.req_wstrb=strobes;
        d.rsp_ready=0; eval(); require(d.req_ready && !d.rsp_valid,"unconsumed prior response");
        tick(); ++transactions; d.req_valid=0;
        // Public pins may change after acceptance. The endpoint must execute
        // the captured request and hold off new acceptance until completion.
        d.req_addr=0xffff; d.req_wdata=0xbad0bad0; d.req_wstrb=15; d.req_write=!write;
        for (unsigned wait=0;!d.rsp_valid;wait++) {
            require(wait<8,"response latency timeout");
            eval(); require(!d.req_ready,"accepted request while prior execution pending"); tick();
        }
        const uint32_t data=d.rsp_rdata; const bool error=d.rsp_error;
        require(!write || data==0,"write response must be zero");
        require(!error || data==0,"failed read leaks stale data");
        for (unsigned i=0,count=random()%4;i<count;i++) {
            eval(); require(!d.req_ready,"accepted request without response credit");
            require(d.rsp_valid && d.rsp_rdata==data && bool(d.rsp_error)==error,"response changed under backpressure");
            tick(); ++response_stalls;
        }
        d.rsp_ready=1; tick(); d.rsp_ready=0;
        require(!d.rsp_valid,"response not consumed");
        if (error) ++access_errors;
        return {data,error};
    }
    uint32_t read(unsigned address) {
        auto result=transfer(false,address,0,0); require(!result.second,"read rejected at "+std::to_string(address));
        return result.first;
    }
    void write(unsigned address,uint32_t data,unsigned strobes=15,bool expect_error=false) {
        auto result=transfer(true,address,data,uint8_t(strobes));
        require(result.second==expect_error,"unexpected write response at "+std::to_string(address));
    }
    void clear_errors() { write(NPU128_ERRORS,31); require(read(NPU128_ERRORS)==0,"sticky error clear"); }
    void stream_writes(const std::vector<std::pair<unsigned,uint32_t>>& packets) {
        unsigned sent=0,received=0,wait=0;
        d.rsp_ready=1;
        while (received<packets.size()) {
            require(++wait<packets.size()*8+20,"ordered write stream timeout");
            d.req_valid=sent<packets.size(); d.req_write=1; d.req_wstrb=15;
            if (d.req_valid) { d.req_addr=packets[sent].first; d.req_wdata=packets[sent].second; }
            eval();
            if (d.rsp_valid) { require(!d.rsp_error && d.rsp_rdata==0,"ordered stream response"); ++received; }
            if (d.req_valid && d.req_ready) { ++sent; ++transactions; }
            tick();
        }
        d.req_valid=0; d.rsp_ready=0;
        require(sent==packets.size() && !d.rsp_valid,"extra ordered write response");
    }
    Task make(unsigned w,unsigned h,unsigned ci,unsigned co,unsigned k,unsigned s,unsigned p) {
        Task t{w,h,ci,co,k,s,p,{},{}};
        t.image.resize(w*h*ci); t.weight.resize(co*ci*k*k);
        for (auto& v:t.image) v=int8_t(int(random()%31)-15);
        for (auto& v:t.weight) v=int8_t(int(random()%29)-14);
        for (unsigned oc=0;oc<16;oc++) {
            t.bias[oc]=uint32_t(int(random()%1001)-500);
            t.multiplier[oc]=(oc+1)*7349; t.shift[oc]=17+oc%5;
        }
        return t;
    }
    void activations(const Task& t,unsigned bank) {
        for (unsigned pixel=0;pixel<t.w*t.h;pixel++) for (unsigned group=0;group<t.groups();group++) {
            uint64_t word=0;
            for (unsigned lane=0;lane<8;lane++) {
                unsigned ic=group*8+lane;
                word|=uint64_t(ic<t.ci ? uint8_t(t.image[pixel*t.ci+ic]) : 0xa7)<<(lane*8);
            }
            require(npu128_activation(&io,bank,pixel*t.groups()+group,word)==0,"activation helper");
        }
    }
    void weights(const Task& t) {
        for (unsigned ky=0;ky<t.k;ky++) for (unsigned kx=0;kx<t.k;kx++) for (unsigned group=0;group<t.groups();group++) {
            std::array<uint32_t,32> words{};
            for (unsigned oc=0;oc<16;oc++) for (unsigned lane=0;lane<8;lane++) {
                unsigned ic=group*8+lane,byte=oc*8+lane;
                uint8_t v=oc<t.co && ic<t.ci ? uint8_t(t.weight[t.wi(oc,ic,ky,kx)]) : 0x5a;
                words[byte/4]|=uint32_t(v)<<(8*(byte%4));
            }
            require(npu128_weight(&io,(ky*t.k+kx)*t.groups()+group,words.data())==0,"weight helper");
        }
    }
    void descriptor(const Task& t,unsigned mode,unsigned bank) {
        write(NPU128_SHAPE,npu128_shape(t.w,t.h,t.ci));
        write(NPU128_OPTIONS,npu128_options(t.co,t.k==3,t.s==2,t.p,mode,bank));
        for (unsigned oc=0;oc<16;oc++) write(NPU128_BIAS+oc*4,t.bias[oc]);
    }
    void load(const Task& t,unsigned mode,unsigned bank) {
        clear_errors();
        for (unsigned oc=0;oc<16;oc++) {
            require(npu128_configure(&io,0,oc,t.multiplier[oc])==0,"multiplier config");
            require(npu128_configure(&io,1,oc,t.shift[oc])==0,"shift config");
        }
        activations(t,bank); weights(t); descriptor(t,mode,bank);
    }
    void lut() {
        for (unsigned i=0;i<256;i++) require(npu128_configure(&io,2,i,table[i])==0,"LUT config");
        require(read(NPU128_STATUS)&NPU128_LUT_READY,"LUT ready");
    }
    void check_output(const Task& t,unsigned mode,unsigned pixel,const npu128_output& out) {
        const uint32_t expected_meta=((1u<<t.co)-1) | ((pixel%t.ow())<<16) | ((pixel/t.ow())<<24);
        require(out.meta==expected_meta,"output coordinates/mask");
        require(out.flags==(8|mode|((pixel+1==t.ow()*t.oh()) ? 4 : 0)),"output mode/last");
        for (unsigned oc=0;oc<16;oc++) {
            const uint32_t raw=t.reference(pixel,oc);
            require(out.raw[oc]==raw,"scalar raw mismatch pixel "+std::to_string(pixel)+" channel "+std::to_string(oc));
            uint8_t q=uint8_t(requant_reference(signed32(raw),t.multiplier[oc],t.shift[oc]));
            const uint8_t expected=oc>=t.co || mode==0 ? 0 : mode==2 ? table[q] : q;
            require(uint8_t(out.quantized[oc/4]>>(8*(oc%4)))==expected,"quantized/LUT result mismatch");
        }
        ++pixels;
    }
    void drain(const Task& t,unsigned mode,bool inject_read_failure=false) {
        const uint64_t deadline=cycles+uint64_t(t.ow()*t.oh())*(t.groups()*t.k*t.k+200)+1000;
        for (unsigned pixel=0;pixel<t.ow()*t.oh();pixel++) {
            while (!(read(NPU128_STATUS)&NPU128_OUTPUT_VALID)) require(cycles<deadline,"output timeout");
            if (inject_read_failure && pixel==0) {
                fail_read=NPU128_OUTPUT_RAW+8;
                npu128_output ignored{};
                require(npu128_read_output(&io,&ignored)==-9,"transport failure not returned");
                require(read(NPU128_STATUS)&NPU128_OUTPUT_VALID,"failed read popped output");
            }
            npu128_output out{};
            require(npu128_read_output(&io,&out)==1,"output helper");
            check_output(t,mode,pixel,out);
        }
        uint32_t status=0;
        do { status=read(NPU128_STATUS); require(cycles<deadline,"done timeout"); } while (status&NPU128_BUSY);
        require((status&(NPU128_DONE|NPU128_OUTPUT_VALID|NPU128_ERROR))==NPU128_DONE,"final status");
        require(read(NPU128_OUTPUT_COUNT)==t.ow()*t.oh(),"captured output count");
        require(read(NPU128_PERF_MAC)==t.ow()*t.oh()*t.groups()*t.k*t.k,"MAC count");
        require(read(NPU128_PERF_TOTAL)>=read(NPU128_PERF_CONV),"performance counters");
        idle(10); require(read(NPU128_STATUS)&NPU128_DONE,"done must be sticky");
        write(NPU128_STATUS,NPU128_DONE); require(!(read(NPU128_STATUS)&NPU128_DONE),"done W1C");
        ++tasks;
    }
    void run(const Task& t,unsigned mode,unsigned bank) {
        load(t,mode,bank); write(NPU128_CONTROL,NPU128_START);
        const uint64_t first_deadline=cycles+t.groups()*t.k*t.k+200;
        while (!(read(NPU128_STATUS)&NPU128_OUTPUT_VALID))
            require(cycles<first_deadline,"first output timeout Ci="+std::to_string(t.ci));
        idle(250);
        require(read(NPU128_STATUS)&NPU128_OUTPUT_VALID,"held output never appeared");
        const uint32_t first=read(NPU128_OUTPUT_RAW),meta=read(NPU128_OUTPUT_META);
        idle(80);
        require(read(NPU128_OUTPUT_RAW)==first && read(NPU128_OUTPUT_META)==meta,"unread output overwritten");
        require(read(NPU128_OUTPUT_COUNT)==1,"capture window overflow");
        if (t.ow()*t.oh()>1) {
            require(!(read(NPU128_STATUS)&NPU128_DONE),"done before last output delivery");
            require(read(NPU128_PERF_POST)>0,"no output backpressure reached NPU");
        }
        drain(t,mode,true);
    }
    void bus_pipeline() {
        const std::array<unsigned,4> addresses{NPU128_ACT_DATA,NPU128_ACT_DATA+4,NPU128_BIAS+60,NPU128_WEIGHT_DATA+124};
        std::map<unsigned,uint32_t> model;
        for (auto a:addresses) { write(a,0); model[a]=0; }
        struct Packet { bool write; unsigned address; uint32_t value; unsigned strobes; };
        std::vector<Packet> packets;
        for (unsigned i=0;i<300;i++) packets.push_back({bool(random()%2),addresses[random()%4],uint32_t(random()),unsigned(random()%16)});
        std::deque<uint32_t> expected;
        unsigned sent=0,received=0,iterations=0;
        bool active=false,held=false;
        uint32_t held_data=0;
        while (received<packets.size()) {
            require(++iterations<5000,"pipelined bus timeout");
            if (!active && sent<packets.size() && random()%3) active=true;
            d.req_valid=active;
            if (active) { const auto& p=packets[sent]; d.req_write=p.write; d.req_addr=p.address; d.req_wdata=p.value; d.req_wstrb=p.strobes; }
            d.rsp_ready=random()%3!=0; eval();
            if (held) require(d.rsp_valid && !d.rsp_error && d.rsp_rdata==held_data,"pipelined stalled response changed");
            if (d.rsp_valid && d.rsp_ready) {
                require(!expected.empty() && !d.rsp_error && d.rsp_rdata==expected.front(),"pipelined response ordering");
                expected.pop_front(); ++received;
            }
            held=d.rsp_valid && !d.rsp_ready; held_data=d.rsp_rdata;
            if (held) ++response_stalls;
            if (d.req_valid && d.req_ready) {
                const auto& p=packets[sent];
                expected.push_back(p.write ? 0 : model[p.address]);
                if (p.write) for (unsigned byte=0;byte<4;byte++) if (p.strobes&(1u<<byte)) {
                    uint32_t mask=0xffu<<(byte*8);
                    model[p.address]=(model[p.address]&~mask)|(p.value&mask);
                }
                ++sent; ++transactions; active=false;
            }
            tick();
        }
        d.req_valid=0; d.rsp_ready=0;
        require(expected.empty() && !d.rsp_valid,"extra bus response");
        for (auto a:addresses) require(read(a)==model[a],"pipelined byte write side effect");
    }
    void register_errors() {
        require(read(NPU128_ID)==0x4e505531 && read(NPU128_ABI)==0x10000,"device identity");
        require(read(NPU128_ACT_WORDS)==24576 && read(NPU128_WEIGHT_WORDS)==864,"capacity registers");
        write(NPU128_ACT_DATA,0x11223344); write(NPU128_ACT_DATA,0xaabbccdd,5);
        require(read(NPU128_ACT_DATA)==0x11bb33dd,"byte strobes");
        write(NPU128_ACT_DATA,0,0); require(read(NPU128_ACT_DATA)==0x11bb33dd,"zero strobes changed register");
        write(NPU128_CONTROL,NPU128_START,0); require(!(read(NPU128_STATUS)&NPU128_BUSY),"zero strobe started task");
        write(NPU128_ACT_DATA+1,0,15,true); write(0x1060,0,15,true); write(NPU128_ID,0,15,true);
        require(transfer(false,0xffff,0,0).second,"unaligned read accepted");
        require(read(NPU128_ACT_DATA)==0x11bb33dd,"bad address aliased register");
        require(read(NPU128_ERRORS)==NPU128_ERR_ADDRESS,"address error bit"); clear_errors();
        write(NPU128_ACT_INDEX,24576); write(NPU128_ACT_COMMIT,1,15,true);
        write(NPU128_WEIGHT_INDEX,864); write(NPU128_WEIGHT_COMMIT,1,15,true);
        write(NPU128_CFG_INDEX,0x400); write(NPU128_CFG_COMMIT,1,15,true);
        write(NPU128_CONTROL,3,15,true); write(NPU128_OPTIONS,0x800,15,true);
        require(read(NPU128_ERRORS)==NPU128_ERR_RANGE,"range errors"); clear_errors();
        write(NPU128_OUTPUT_POP,1,15,true); require(transfer(false,NPU128_OUTPUT_RAW,0,0).second,"empty output read accepted");
        require(read(NPU128_ERRORS)==NPU128_ERR_STATE,"empty state errors"); clear_errors();
        const uint64_t before=transactions;
        require(npu128_activation(&io,2,0,0)==-2 && npu128_activation(&io,0,24576,0)==-2,"helper bounds");
        require(transactions==before,"invalid helper issued transport");
        bus_pipeline();
    }
    void invalid_devices() {
        write(NPU128_CONTROL,NPU128_RESET);
        require(npu128_configure(&io,3,0,0)==-3,"invalid config kind"); clear_errors();
        require(npu128_configure(&io,0,0,0x80000000)==-3,"invalid multiplier"); clear_errors();
        require(npu128_configure(&io,1,0,64)==-3,"invalid shift"); clear_errors();
        require(npu128_configure(&io,2,5,0)==-3,"out-of-order LUT load"); clear_errors();
        auto t=make(3,3,8,16,3,1,1);
        for (unsigned variant=0;variant<5;variant++) {
            descriptor(t,variant==0 ? 2 : variant==1 ? 3 : 0,0);
            if (variant==2) write(NPU128_SHAPE,npu128_shape(0,3,8));
            if (variant==3) write(NPU128_SHAPE,npu128_shape(3,3,769));
            if (variant==4) write(NPU128_OPTIONS,0);
            write(NPU128_CONTROL,NPU128_START);
            idle(5);
            require(!(read(NPU128_STATUS)&(NPU128_BUSY|NPU128_DONE|NPU128_OUTPUT_VALID)),"invalid command executed");
            require(read(NPU128_ERRORS)==NPU128_ERR_COMMAND,"invalid command error"); clear_errors();
        }
        // A config response is delayed until the core has consumed its pulse.
        // Its error must be visible before a later, ordered W1C clears it.
        write(NPU128_CFG_INDEX,3<<8); write(NPU128_CFG_DATA,0);
        write(NPU128_CFG_COMMIT,1);
        require(read(NPU128_ERRORS)==NPU128_ERR_CONFIG,"config error absent after commit response");
        write(NPU128_ERRORS,NPU128_ERR_CONFIG);
        require(read(NPU128_ERRORS)==0,"ordered config error W1C");
    }
    void prefetch_and_protection() {
        auto current=make(5,4,17,13,3,1,1),next=make(3,2,17,13,3,1,1);
        next.weight=current.weight; next.bias=current.bias; next.multiplier=current.multiplier; next.shift=current.shift;
        next.bias[0]^=0xa5a5ffffu;
        load(current,2,0); write(NPU128_CONTROL,NPU128_START); idle(150);
        descriptor(next,2,1); // staging a future descriptor must not change active bank protection.
        write(NPU128_ACT_INDEX,0); write(NPU128_ACT_DATA,0xffffffff); write(NPU128_ACT_DATA+4,0xffffffff);
        write(NPU128_ACT_COMMIT,1,15,true);
        write(NPU128_WEIGHT_INDEX,0);
        for (unsigned word=0;word<32;word++) write(NPU128_WEIGHT_DATA+word*4,0x7f7f7f7f);
        write(NPU128_CFG_INDEX,0); write(NPU128_CFG_DATA,0);
        write(NPU128_WEIGHT_COMMIT,1,15,true); write(NPU128_CFG_COMMIT,1,15,true);
        write(NPU128_CONTROL,NPU128_START,15,true);
        require(read(NPU128_ERRORS)==NPU128_ERR_STATE,"active access protection"); clear_errors();
        activations(next,1);
        require(read(NPU128_STATUS)&NPU128_BUSY,"prefetch did not overlap active task");
        drain(current,2);
        write(NPU128_CONTROL,NPU128_START); drain(next,2);
        // Last vector can be captured with DONE high, but must still block a new START.
        auto one=make(1,1,8,1,1,1,0); load(one,0,0); write(NPU128_CONTROL,NPU128_START); idle(100);
        require((read(NPU128_STATUS)&(NPU128_DONE|NPU128_OUTPUT_VALID))==(NPU128_DONE|NPU128_OUTPUT_VALID),"single final capture status");
        write(NPU128_CONTROL,NPU128_START,15,true); clear_errors(); drain(one,0);
    }
    void reset_recovery() {
        auto t=make(5,4,17,13,3,1,1);
        for (unsigned hard=0;hard<2;hard++) {
            load(t,2,hard); write(NPU128_CONTROL,NPU128_START); idle(100);
            require(read(NPU128_STATUS)&NPU128_OUTPUT_VALID,"abort needs pending output");
            if (hard) {
                d.req_valid=1; d.req_write=0; d.req_addr=NPU128_OUTPUT_RAW; d.rsp_ready=0; tick(); ++transactions;
                d.req_valid=0;
                for (unsigned wait=0;!d.rsp_valid;wait++) { require(wait<8,"reset test response timeout"); tick(); }
                require(d.rsp_valid,"reset test response"); hard_reset();
            } else write(NPU128_CONTROL,NPU128_RESET);
            idle(100);
            require(read(NPU128_STATUS)==0 && read(NPU128_ERRORS)==0 && read(NPU128_OUTPUT_COUNT)==0,"reset did not clear status");
            require(read(NPU128_ACT_DATA)==0 && read(NPU128_WEIGHT_DATA+124)==0 && read(NPU128_BIAS)==0,"reset staging");
            require(read(NPU128_PERF_MAC)==0 && read(NPU128_PERF_TOTAL)==0,"reset counters");
            // RAM content is retained; valid/configuration state is reset.
            auto retained=t; retained.bias.fill(0);
            descriptor(retained,0,hard); write(NPU128_CONTROL,NPU128_START); drain(retained,0);
            ++aborts; lut();
        }
    }
    void ordered_commands() {
        auto old=make(1,1,8,16,1,1,0),next=make(1,1,8,16,1,1,0);
        load(old,0,0);
        uint64_t image=0;
        for (unsigned lane=0;lane<8;lane++) image|=uint64_t(uint8_t(next.image[lane]))<<(8*lane);
        std::vector<std::pair<unsigned,uint32_t>> packets{
            {NPU128_ACT_INDEX,0},{NPU128_ACT_DATA,uint32_t(image)},
            {NPU128_ACT_DATA+4,uint32_t(image>>32)},{NPU128_ACT_COMMIT,1},
            {NPU128_ACT_DATA,0x7f7f7f7f},{NPU128_ACT_INDEX,24575},{NPU128_WEIGHT_INDEX,0}};
        for (unsigned word=0;word<32;word++) {
            uint32_t value=0;
            for (unsigned lane=0;lane<4;lane++) value|=uint32_t(uint8_t(next.weight[word*4+lane]))<<(8*lane);
            packets.push_back({NPU128_WEIGHT_DATA+word*4,value});
        }
        packets.insert(packets.end(),{{NPU128_WEIGHT_COMMIT,1},{NPU128_WEIGHT_DATA,0x7f7f7f7f},
            {NPU128_WEIGHT_INDEX,863},{NPU128_CONTROL,NPU128_RESET}});
        // Every next request is already presented while the preceding one is
        // pending; RESET must retain both acknowledged memory commits.
        stream_writes(packets);
        next.bias.fill(0); descriptor(next,0,0);
        stream_writes({{NPU128_CONTROL,NPU128_START},{NPU128_SHAPE,0},
            {NPU128_OPTIONS,0},{NPU128_BIAS,0x12345678}});
        drain(next,0);
        // External reset cancels an accepted but unacknowledged commit. It
        // must not write poison into RAM or leave a response/pulse behind.
        write(NPU128_ACT_INDEX,0); write(NPU128_ACT_DATA,0x7f7f7f7f);
        d.req_valid=1; d.req_write=1; d.req_addr=NPU128_ACT_COMMIT; d.req_wdata=1; d.req_wstrb=15;
        eval(); require(d.req_ready,"reset cancellation request ready"); tick(); ++transactions;
        require(!d.rsp_valid,"commit acknowledged before execution"); hard_reset();
        descriptor(next,0,0); write(NPU128_CONTROL,NPU128_START); drain(next,0); ++aborts;
    }
    void test() {
        register_errors(); invalid_devices(); ordered_commands(); lut();
        for (unsigned mode=0;mode<3;mode++) {
            run(make(5,4,17,13,3,1,1),mode,mode%2);
            run(make(5,3,9,16,1,2,0),mode,1-mode%2);
            run(make(3,3,1,1,3,2,1),mode,mode%2);
        }
        // Both full activation banks, including 8191/8192 and 16383/16384.
        for (unsigned bank=0;bank<2;bank++) run(make(16,16,768,1,1,1,0),0,bank);
        // All 864 weight addresses, including the highest physical word.
        run(make(3,3,768,3,3,1,0),2,1);
        auto wrap=make(1,1,8,16,1,1,0);
        wrap.image.assign(8,127); wrap.weight.assign(128,127); wrap.bias.fill(0x7fffffff);
        run(wrap,0,0); run(wrap,2,1);
        prefetch_and_protection(); reset_recovery();
        require(read(NPU128_ERRORS)==0,"final errors");
    }
};
int main() {
    try {
        Bench b; b.test();
        std::cout<<"PASS npu_mmio128: "<<b.tasks<<" tasks, "<<b.pixels<<" scalar-checked output vectors, "
                 <<b.transactions<<" public-bus transactions, "<<b.response_stalls<<" response stall cycles, "
                 <<b.access_errors<<" rejected accesses, "<<b.aborts<<" reset recoveries; three modes, full banks/weights, prefetch, output backpressure\n";
    } catch (const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}

#include "Vuart_mmio_bridge.h"
#include "verilated.h"
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <vector>
static void check(bool ok,const char* s){if(!ok)throw std::runtime_error(s);}
int main(){
    try {
        VerilatedContext ctx;ctx.threads(1);Vuart_mmio_bridge d{&ctx};
        d.req_ready=0;d.tx_ready=0;d.rsp_valid=0;d.rsp_error=0;d.rsp_rdata=0;d.rx_valid=0;d.rx_error=0;
        auto tick=[&](){d.clk=0;d.eval();d.clk=1;d.eval();};
        auto reset=[&](){d.rst=1;tick();tick();d.rst=0;d.clk=0;d.eval();};
        std::vector<uint8_t> frame{0xa5,0x5a,1,7,1,15,0x60,0,0xef,0xbe,0xad,0xde};
        uint16_t c=0xffff;
        for(unsigned i=2;i<12;i++){c^=uint16_t(frame[i])<<8;for(unsigned b=0;b<8;b++)c=uint16_t((c<<1)^((c&0x8000)?0x1021:0));}
        frame.push_back(uint8_t(c));frame.push_back(uint8_t(c>>8));
        auto feed=[&](){for(auto b:frame){d.rx_valid=1;d.rx_data=b;tick();}d.rx_valid=0;};
        auto quiet=[&](unsigned n){for(unsigned i=0;i<n;i++){tick();check(!d.req_valid&&!d.tx_valid,"quarantined suffix caused activity");}};
        reset();d.rx_error=1;tick();d.rx_error=0;
        feed();check(!d.req_valid&&!d.tx_valid,"nested frame escaped quarantine");
        quiet(31);feed();check(!d.req_valid&&!d.tx_valid,"incoming bytes did not extend required quiet interval");
        quiet(40);feed();tick();
        check(d.req_valid&&d.req_write&&d.req_addr==0x60&&d.req_wdata==0xdeadbeef,"quiet interval did not recover parser");
        reset();d.rx_error=1;tick();d.rx_error=0;reset();feed();tick();
        check(d.req_valid,"external reset did not clear quarantine");
        std::cout<<"PASS framing quarantine: nested valid frame ignored; quiet interval extends on bytes; recovery and hard reset\n";
    } catch(const std::exception& e){std::cerr<<"FAIL "<<e.what()<<'\n';return 1;}
}

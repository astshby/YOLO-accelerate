// Public-pin UART simulator used by the same Python client as a physical port.
#include "Vnpu_uart_board.h"
#include "verilated.h"
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <vector>
#ifndef UART_CLOCK_HZ
#define UART_CLOCK_HZ 27000000
#endif

class Board {
    static VerilatedContext* single(VerilatedContext& c) { c.threads(1); return &c; }
    unsigned host_state=0,host_timer=0,host_bit=0;
    uint8_t host_byte=0;
    bool previous_tx=true;
public:
    static constexpr unsigned divisor=(UART_CLOCK_HZ+57600)/115200,interbyte_timeout=UART_CLOCK_HZ/10;
    VerilatedContext context;
    Vnpu_uart_board d{single(context)};
    uint64_t cycles=0;
    std::vector<uint8_t> received;
    Board() { d.uart_rxd=1; d.board_reset_n=1; d.clk_27m=0; d.eval(); reset(); }
    void tick() {
        d.clk_27m=0; d.eval(); d.clk_27m=1; d.eval(); ++cycles;
        const bool line=d.uart_txd;
        if (!d.board_reset_n) { host_state=0; previous_tx=true; received.clear(); return; }
        if (host_state==0) {
            if (previous_tx && !line) { host_state=1; host_timer=divisor/2-1; }
        } else if (host_timer) --host_timer;
        else if (host_state==1) {
            if (line) throw std::runtime_error("FPGA TX false start");
            host_timer=divisor-1; host_bit=0; host_byte=0; host_state=2;
        } else if (host_state==2) {
            if (line) host_byte|=uint8_t(1u<<host_bit);
            host_timer=divisor-1;
            if (++host_bit==8) host_state=3;
        } else {
            if (!line) throw std::runtime_error("FPGA TX invalid stop bit");
            received.push_back(host_byte); host_state=0;
        }
        previous_tx=line;
    }
    void idle(unsigned count) { d.uart_rxd=1; for (unsigned i=0;i<count;i++) tick(); }
    void reset() {
        d.board_reset_n=0; d.eval(); idle(5);
        d.board_reset_n=1; d.eval(); idle(10); received.clear();
    }
    void send_byte(uint8_t data,int delta,bool bad_stop) {
        const unsigned clocks=unsigned(int(divisor)+delta);
        if (clocks<divisor*95/100 || clocks>divisor*105/100) throw std::runtime_error("unsupported test baud deviation");
        d.uart_rxd=0; for (unsigned i=0;i<clocks;i++) tick();
        for (unsigned bit=0;bit<8;bit++) {
            d.uart_rxd=(data>>bit)&1;
            for (unsigned i=0;i<clocks;i++) tick();
        }
        d.uart_rxd=!bad_stop; for (unsigned i=0;i<clocks;i++) tick();
    }
    std::vector<uint8_t> exchange(const std::vector<uint8_t>& bytes,int delta,int bad_stop) {
        received.clear();
        for (unsigned i=0;i<bytes.size();i++) send_byte(bytes[i],delta,int(i)==bad_stop);
        d.uart_rxd=1;
        const uint64_t deadline=cycles+interbyte_timeout+divisor*171;
        while (received.size()<12 && cycles<deadline) tick();
        if (received.empty()) return {};
        if (received.size()!=12) throw std::runtime_error("truncated FPGA reply");
        return received;
    }
};
int main() {
    try {
        Board b;
        std::cout<<"READY "<<Board::divisor<<' '<<Board::interbyte_timeout<<'\n'<<std::flush;
        std::string line;
        while (std::getline(std::cin,line)) {
            std::istringstream input(line); char op; input>>op;
            if (op=='Q') { std::cout<<"BYE "<<b.cycles<<'\n'<<std::flush; return 0; }
            if (op=='R') { b.reset(); std::cout<<"OK\n"<<std::flush; continue; }
            if (op=='I') {
                unsigned count; input>>count;
                if (!input || count>10000000) throw std::runtime_error("bad idle count");
                b.idle(count); std::cout<<"OK\n"<<std::flush; continue;
            }
            if (op=='G') {
                b.received.clear();
                b.d.uart_rxd=0; for (unsigned i=0;i<8;i++) b.tick(); b.idle(2*Board::divisor);
                if (!b.received.empty()) throw std::runtime_error("glitch generated reply");
                std::cout<<"OK\n"<<std::flush; continue;
            }
            if (op!='X') throw std::runtime_error("bad simulator operation");
            std::string hex; int delta=0,bad_stop=-1; input>>hex; input>>delta>>bad_stop;
            if (hex.size()%2 || hex.size()>512) throw std::runtime_error("bad hex request");
            std::vector<uint8_t> bytes;
            for (unsigned i=0;i<hex.size();i+=2) bytes.push_back(uint8_t(std::stoul(hex.substr(i,2),nullptr,16)));
            auto reply=b.exchange(bytes,delta,bad_stop);
            if (reply.empty()) std::cout<<"NONE";
            else for (auto byte:reply) std::cout<<std::hex<<std::setw(2)<<std::setfill('0')<<unsigned(byte);
            std::cout<<std::dec<<'\n'<<std::flush;
        }
    } catch (const std::exception& e) { std::cerr<<"FAIL UART simulator: "<<e.what()<<'\n'; return 1; }
}

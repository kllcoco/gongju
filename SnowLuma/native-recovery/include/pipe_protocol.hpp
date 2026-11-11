#pragma once
#include <cstdint>
#include <span>
#include <vector>
#include <string>
namespace recovery::pipe {
constexpr uint32_t magic=0x31504851,commandLimit=4096,messageLimit=65536,bodyLimit=16*1024*1024;
constexpr uint16_t version=1;constexpr size_t headerSize=40;
enum class Op:uint16_t {Hello=1,SendRequest=2,SendAck=3,SendReply=4,Error=5,Receive=6,Login=7,Identity=17,Receipt=21,State=22};
struct Frame {uint16_t op{};uint32_t request{},flags{};int32_t status{};uint64_t value{};std::string command,message;std::vector<uint8_t> body;};
std::vector<uint8_t> encode(const Frame&);
class Reader {
    std::vector<uint8_t> buffer;size_t expected{headerSize};bool failed{};
public:
    std::vector<Frame> push(std::span<const uint8_t>);
    size_t buffered()const{return buffer.size();}
};
}

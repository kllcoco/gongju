#include "message_snapshot.hpp"
#include "varint.hpp"
#include <stdexcept>
namespace recovery::snapshot {
namespace {
uint64_t little(std::span<const uint8_t> bytes){
    uint64_t value=0;for(size_t i=0;i<bytes.size();i++)value|=uint64_t(bytes[i])<<(8*i);return value;
}
}
uint64_t field(std::span<const uint8_t> records,uint32_t id){
    if(records.size()%12)throw std::runtime_error("message snapshot record size rejected");
    for(size_t i=0;i<records.size();i+=12)
        if(little(records.subspan(i,4))==id)return little(records.subspan(i+4,8));
    throw std::runtime_error("message snapshot field missing");
}
// Hook RVA 0x8e30: tags 3/5, two high bits and low bit clear, 32-bit payload.
uint32_t integer(uint64_t tagged){
    auto type=(tagged>>58)&15;
    if((type!=3&&type!=5)||(tagged&0xc000000000000001ULL))throw std::runtime_error("message snapshot integer rejected");
    if(tagged&0x00fffffe00000000ULL)throw std::runtime_error("message snapshot integer too large");
    return uint32_t(tagged>>1);
}
uint64_t uid_address(uint64_t tagged){
    if((tagged>>56)!=0xa0)throw std::runtime_error("message snapshot UID type rejected");
    return tagged&0x00ffffffffffffffULL;
}
// Hook RVA 0x8f10. The original also rereads and compares process memory;
// this function decodes an immutable snapshot and does not replace that lock/recheck.
std::string uid(std::span<const uint8_t> encoded){
    auto outer=wire::decode(encoded);
    if(outer.value>=0x83)throw std::runtime_error("message snapshot UID size rejected");
    auto inner=wire::decode(encoded,outer.consumed);
    if(inner.value<3||inner.value>128||outer.value!=inner.value+inner.consumed)
        throw std::runtime_error("message snapshot UID bounds rejected");
    size_t begin=outer.consumed+inner.consumed;
    if(encoded.size()!=outer.consumed+outer.value)throw std::runtime_error("message snapshot UID bounds rejected");
    auto bytes=encoded.subspan(begin,size_t(inner.value));
    if(bytes[0]!='u'||bytes[1]!='_')throw std::runtime_error("message snapshot UID prefix rejected");
    for(uint8_t b:bytes)if(!((b>='A'&&b<='Z')||(b>='a'&&b<='z')||(b>='0'&&b<='9')||b=='_'||b=='-'))
        throw std::runtime_error("message snapshot UID character rejected");
    return std::string(bytes.begin(),bytes.end());
}
}

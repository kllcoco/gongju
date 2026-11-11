#pragma once
#include <cstdint>
#include <span>
#include <stdexcept>
namespace recovery::wire {
struct Varint {uint64_t value;uint32_t consumed;};
// Reconstructed from Hook RVAs 0x9c60 and 0x9e30. Noncanonical encodings
// are accepted, matching the observed branch structure. Exception ABI differs.
inline Varint decode(std::span<const uint8_t> bytes,size_t offset=0){
    if(offset>bytes.size())throw std::out_of_range("varint offset");
    bytes=bytes.subspan(offset);uint64_t value=0;
    for(size_t i=0;i<10;i++){
        if(i>=bytes.size())throw std::out_of_range("truncated varint");
        uint8_t b=bytes[i];
        if(i==9&&b>1)throw std::overflow_error("varint overflow");
        value|=uint64_t(b&0x7f)<<(i*7);
        if(!(b&0x80))return {value,uint32_t(i+1)};
    }
    throw std::overflow_error("varint overflow");
}
}

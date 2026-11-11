#include "pipe_protocol.hpp"
#include <stdexcept>
#include <algorithm>
#include <cstring>
namespace recovery::pipe {
namespace {
uint64_t read(const uint8_t* p,size_t n){uint64_t v=0;for(size_t i=0;i<n;i++)v|=uint64_t(p[i])<<(8*i);return v;}
void write(uint8_t* p,uint64_t v,size_t n){for(size_t i=0;i<n;i++)p[i]=uint8_t(v>>(8*i));}
void lengths(size_t c,size_t m,size_t b){if(c>commandLimit||m>messageLimit||b>bodyLimit)throw std::runtime_error("pipe frame length exceeds limit");}
}
std::vector<uint8_t> encode(const Frame& f){
    lengths(f.command.size(),f.message.size(),f.body.size());std::vector<uint8_t> result(headerSize+f.command.size()+f.message.size()+f.body.size());auto p=result.data();
    write(p,magic,4);write(p+4,version,2);write(p+6,f.op,2);write(p+8,f.request,4);write(p+12,uint32_t(f.status),4);write(p+16,f.flags,4);
    write(p+20,f.command.size(),4);write(p+24,f.message.size(),4);write(p+28,f.body.size(),4);write(p+32,f.value,8);
    auto pos=result.begin()+headerSize;pos=std::copy(f.command.begin(),f.command.end(),pos);pos=std::copy(f.message.begin(),f.message.end(),pos);std::copy(f.body.begin(),f.body.end(),pos);return result;
}
std::vector<Frame> Reader::push(std::span<const uint8_t> input){
    if(failed)throw std::runtime_error("pipe parser is in failed state");std::vector<Frame> frames;
    try{while(!input.empty()){
        size_t n=std::min(input.size(),expected-buffer.size());buffer.insert(buffer.end(),input.begin(),input.begin()+n);input=input.subspan(n);
        if(buffer.size()<expected)continue;
        auto p=buffer.data();if(expected==headerSize){
            if(read(p,4)!=magic||read(p+4,2)!=version)throw std::runtime_error("bad pipe frame header");
            auto c=read(p+20,4),m=read(p+24,4),b=read(p+28,4);lengths(c,m,b);expected=headerSize+size_t(c+m+b);if(buffer.size()<expected)continue;
        }
        Frame f;f.op=uint16_t(read(p+6,2));f.request=uint32_t(read(p+8,4));f.status=int32_t(uint32_t(read(p+12,4)));f.flags=uint32_t(read(p+16,4));f.value=read(p+32,8);
        size_t c=size_t(read(p+20,4)),m=size_t(read(p+24,4));f.command.assign(reinterpret_cast<char*>(p+40),c);f.message.assign(reinterpret_cast<char*>(p+40+c),m);f.body.assign(buffer.begin()+40+c+m,buffer.end());frames.push_back(std::move(f));buffer.clear();expected=headerSize;
    }}catch(...){failed=true;buffer.clear();throw;}return frames;
}
}

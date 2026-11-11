#pragma once
#include "native.hpp"
#include <span>
namespace recovery {
class PeImage {
public:
    std::vector<uint8_t> bytes;
    uint32_t ntOffset{};
    explicit PeImage(std::span<const uint8_t> file);
    static PeImage read(const std::wstring& path);
    IMAGE_NT_HEADERS64& nt(){return *reinterpret_cast<IMAGE_NT_HEADERS64*>(bytes.data()+ntOffset);}
    const IMAGE_NT_HEADERS64& nt()const{return *reinterpret_cast<const IMAGE_NT_HEADERS64*>(bytes.data()+ntOffset);}
    IMAGE_DATA_DIRECTORY directory(size_t i)const;
    uint32_t export_rva(const char* name)const;
    void relocate(uint64_t base);
    void validate_dependencies()const;
    void require_manual_map_supported()const;
    bool contains(uint64_t offset,uint64_t size)const{return offset<=bytes.size()&&size<=bytes.size()-offset;}
    const char* cstring(uint32_t rva)const;
    template<class T> const T* get(uint64_t offset,size_t count=1)const{
        if(count>SIZE_MAX/sizeof(T)||!contains(offset,sizeof(T)*count))throw std::runtime_error("PE range outside image");
        return reinterpret_cast<const T*>(bytes.data()+offset);
    }
};
}

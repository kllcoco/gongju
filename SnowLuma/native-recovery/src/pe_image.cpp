#include "pe_image.hpp"
#include <fstream>
#include <filesystem>
#include <cstring>
#include <algorithm>
namespace recovery {
PeImage PeImage::read(const std::wstring& path){
    std::ifstream f(std::filesystem::path(path),std::ios::binary|std::ios::ate);
    if(!f)throw std::runtime_error("Cannot open DLL");auto size=f.tellg();
    if(size<0||size>128*1024*1024)throw std::runtime_error("DLL file size invalid");
    std::vector<uint8_t> raw(static_cast<size_t>(size));f.seekg(0);if(!f.read(reinterpret_cast<char*>(raw.data()),size))throw std::runtime_error("Cannot read DLL");
    return PeImage(raw);
}
PeImage::PeImage(std::span<const uint8_t> file){
    auto range=[&](uint64_t at,uint64_t n){if(at>file.size()||n>file.size()-at)throw std::runtime_error("PE file range invalid");};
    range(0,sizeof(IMAGE_DOS_HEADER));auto dos=reinterpret_cast<const IMAGE_DOS_HEADER*>(file.data());
    if(dos->e_magic!=IMAGE_DOS_SIGNATURE||dos->e_lfanew<=0)throw std::runtime_error("Bad DOS magic (not a PE)");
    ntOffset=dos->e_lfanew;range(ntOffset,sizeof(IMAGE_NT_HEADERS64));auto header=reinterpret_cast<const IMAGE_NT_HEADERS64*>(file.data()+ntOffset);
    if(header->Signature!=IMAGE_NT_SIGNATURE||header->OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR64_MAGIC||header->FileHeader.Machine!=IMAGE_FILE_MACHINE_AMD64||!(header->FileHeader.Characteristics&IMAGE_FILE_DLL))throw std::runtime_error("Expected x64 PE DLL");
    if(header->FileHeader.SizeOfOptionalHeader<sizeof(IMAGE_OPTIONAL_HEADER64))throw std::runtime_error("PE optional header sizes are invalid");
    auto& opt=header->OptionalHeader;
    if(opt.SizeOfImage<opt.SizeOfHeaders||opt.SizeOfImage>512*1024*1024||!opt.SizeOfHeaders||opt.SizeOfHeaders<ntOffset+sizeof(IMAGE_NT_HEADERS64)||opt.AddressOfEntryPoint>=opt.SizeOfImage)throw std::runtime_error("Invalid PE image size or entry");
    range(0,opt.SizeOfHeaders);bytes.resize(opt.SizeOfImage);memcpy(bytes.data(),file.data(),opt.SizeOfHeaders);
    uint64_t table=uint64_t(ntOffset)+24+header->FileHeader.SizeOfOptionalHeader;range(table,uint64_t(header->FileHeader.NumberOfSections)*sizeof(IMAGE_SECTION_HEADER));
    if(table+uint64_t(header->FileHeader.NumberOfSections)*sizeof(IMAGE_SECTION_HEADER)>opt.SizeOfHeaders)throw std::runtime_error("Section table outside headers");
    for(unsigned i=0;i<header->FileHeader.NumberOfSections;i++){
        auto s=reinterpret_cast<const IMAGE_SECTION_HEADER*>(file.data()+table)+i;
        if(!contains(s->VirtualAddress,std::max(s->Misc.VirtualSize,s->SizeOfRawData)))throw std::runtime_error("PE section outside image");
        range(s->PointerToRawData,s->SizeOfRawData);
        if(s->SizeOfRawData)memcpy(bytes.data()+s->VirtualAddress,file.data()+s->PointerToRawData,s->SizeOfRawData);
    }
    for(size_t i=0;i<std::min<size_t>(opt.NumberOfRvaAndSizes,16);i++)if(i!=IMAGE_DIRECTORY_ENTRY_SECURITY)directory(i);
    export_rva("snowluma_prepare_unload_v1");validate_dependencies();
}
IMAGE_DATA_DIRECTORY PeImage::directory(size_t i)const{
    if(i>=std::min<size_t>(nt().OptionalHeader.NumberOfRvaAndSizes,16))return {};
    auto d=nt().OptionalHeader.DataDirectory[i];
    if((d.Size||d.VirtualAddress)&&(!d.Size||!d.VirtualAddress||!contains(d.VirtualAddress,d.Size)))throw std::runtime_error("Invalid PE data directory");return d;
}
const char* PeImage::cstring(uint32_t at)const{
    auto p=get<char>(at);if(!memchr(p,0,bytes.size()-at))throw std::runtime_error("Unterminated PE string");return p;
}
uint32_t PeImage::export_rva(const char* name)const{
    auto d=directory(IMAGE_DIRECTORY_ENTRY_EXPORT);if(!d.Size)return 0;
    if(d.Size<sizeof(IMAGE_EXPORT_DIRECTORY))throw std::runtime_error("Invalid export directory");auto e=get<IMAGE_EXPORT_DIRECTORY>(d.VirtualAddress);
    auto names=get<DWORD>(e->AddressOfNames,e->NumberOfNames);auto ords=get<WORD>(e->AddressOfNameOrdinals,e->NumberOfNames);auto funcs=get<DWORD>(e->AddressOfFunctions,e->NumberOfFunctions);
    for(DWORD i=0;i<e->NumberOfNames;i++)if(strcmp(cstring(names[i]),name)==0){
        if(ords[i]>=e->NumberOfFunctions)throw std::runtime_error("Invalid export ordinal");DWORD value=funcs[ords[i]];
        if(!value||!contains(value,1)||(value>=d.VirtualAddress&&value-d.VirtualAddress<d.Size))throw std::runtime_error("Invalid prepare-unload export");return value;
    }return 0;
}
void PeImage::validate_dependencies()const{
    auto d=directory(IMAGE_DIRECTORY_ENTRY_IMPORT);
    if(d.Size){bool ended=false;for(uint32_t n=0;n+sizeof(IMAGE_IMPORT_DESCRIPTOR)<=d.Size;n+=sizeof(IMAGE_IMPORT_DESCRIPTOR)){
        auto desc=get<IMAGE_IMPORT_DESCRIPTOR>(uint64_t(d.VirtualAddress)+n);if(!desc->Name){ended=true;break;}
        cstring(desc->Name);uint64_t source=desc->OriginalFirstThunk?desc->OriginalFirstThunk:desc->FirstThunk;
        for(size_t i=0;;i++){
            if(i>65536)throw std::runtime_error("Import table too large");uint64_t thunk=get<IMAGE_THUNK_DATA64>(source,i+1)[i].u1.AddressOfData;
            get<IMAGE_THUNK_DATA64>(desc->FirstThunk,i+1);if(!thunk)break;
            if(!(thunk&IMAGE_ORDINAL_FLAG64)){if(thunk>UINT32_MAX-2)throw std::runtime_error("Invalid import name");cstring(uint32_t(thunk)+2);}
        }
    }if(!ended)throw std::runtime_error("Unterminated import directory");}
    auto exceptions=directory(IMAGE_DIRECTORY_ENTRY_EXCEPTION);if(exceptions.Size%sizeof(RUNTIME_FUNCTION))throw std::runtime_error("Invalid exception directory");
    auto tls=directory(IMAGE_DIRECTORY_ENTRY_TLS);if(tls.Size){
        if(tls.Size<sizeof(IMAGE_TLS_DIRECTORY64))throw std::runtime_error("Truncated TLS directory");
        auto t=get<IMAGE_TLS_DIRECTORY64>(tls.VirtualAddress);if(t->EndAddressOfRawData<t->StartAddressOfRawData)throw std::runtime_error("Invalid TLS range");
        uint64_t preferred=nt().OptionalHeader.ImageBase,rawSize=t->EndAddressOfRawData-t->StartAddressOfRawData;
        if(rawSize&&(t->StartAddressOfRawData<preferred||!contains(t->StartAddressOfRawData-preferred,rawSize)))throw std::runtime_error("TLS data outside image");
        if(t->AddressOfIndex&&(t->AddressOfIndex<preferred||!contains(t->AddressOfIndex-preferred,sizeof(DWORD))))throw std::runtime_error("TLS index outside image");
        if(t->AddressOfCallBacks){if(t->AddressOfCallBacks<preferred)throw std::runtime_error("Invalid TLS callback table");
            uint64_t at=t->AddressOfCallBacks-preferred;for(size_t i=0;;i++){auto callback=get<uint64_t>(at,i+1)[i];if(!callback)break;if(callback<preferred||!contains(callback-preferred,1))throw std::runtime_error("TLS callback outside image");}}
    }
}
void PeImage::require_manual_map_supported()const{
    // Inspection and normal LoadLibrary unload must accept valid static TLS.
    // Only the manual mapping path has these feature restrictions.
    auto tls=directory(IMAGE_DIRECTORY_ENTRY_TLS);
    if(tls.Size){auto t=get<IMAGE_TLS_DIRECTORY64>(tls.VirtualAddress);
        if(t->EndAddressOfRawData!=t->StartAddressOfRawData||t->SizeOfZeroFill)throw std::runtime_error("Static TLS data is not supported by recovered mapper");
    }
    if(directory(IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT).Size)throw std::runtime_error("Delay imports are not supported by recovered mapper");
}
void PeImage::relocate(uint64_t base){
    auto delta=base-nt().OptionalHeader.ImageBase;if(!delta)return;auto d=directory(IMAGE_DIRECTORY_ENTRY_BASERELOC);
    if(!d.Size)throw std::runtime_error("Image at non-preferred base but relocations are stripped");
    for(uint64_t used=0;used<d.Size;){auto block=get<IMAGE_BASE_RELOCATION>(d.VirtualAddress+used);
        if(block->SizeOfBlock<8||block->SizeOfBlock>d.Size-used||(block->SizeOfBlock-8)%2)throw std::runtime_error("Invalid relocation block");
        auto list=get<WORD>(d.VirtualAddress+used+8,(block->SizeOfBlock-8)/2);
        for(size_t i=0;i<(block->SizeOfBlock-8)/2;i++){unsigned type=list[i]>>12;uint64_t at=uint64_t(block->VirtualAddress)+(list[i]&0xfff);
            if(type==IMAGE_REL_BASED_ABSOLUTE)continue;if(type!=IMAGE_REL_BASED_DIR64)throw std::runtime_error("Unsupported relocation type");
            auto value=*get<uint64_t>(at)+delta;memcpy(bytes.data()+at,&value,8);
        }used+=block->SizeOfBlock;
    }
}
}

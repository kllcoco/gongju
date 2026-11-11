#include "pipe_protocol.hpp"
#include "pe_image.hpp"
#include "varint.hpp"
#include "message_snapshot.hpp"
#include <iostream>
#include <cassert>
#include <random>
// Assertions remain active in Release builds.
#ifdef NDEBUG
#undef assert
#define assert(x) do{if(!(x))throw std::runtime_error("check failed: " #x);}while(0)
#endif
int wmain(int argc,wchar_t** argv){
    using namespace recovery;std::mt19937 random(2400);
    auto rejects=[](auto fn){bool caught=false;try{fn();}catch(const std::exception&){caught=true;}assert(caught);};
    const uint8_t uidBytes[]={4,3,'u','_','A'};assert(snapshot::uid(uidBytes)=="u_A");
    auto uidMax=std::vector<uint8_t>{0x82,1,0x80,1,'u','_'};uidMax.resize(132,'a');assert(snapshot::uid(uidMax).size()==128);
    const uint8_t noncanonicalUid[]={0x84,0,3,'u','_','A'};assert(snapshot::uid(noncanonicalUid)=="u_A");
    for(auto invalidUid:std::vector<std::vector<uint8_t>>{
        {},{0x80},{3,2,'u','_'},{4,3,'x','_','A'},{4,3,'u','_','!'},{4,3,'u','_',255},
        {4,3,'u','_'},{4,3,'u','_','A',0},{5,3,'u','_','A'},{0x83,1,0},{4,4,'u','_','A'}})
        rejects([&]{snapshot::uid(invalidUid);});
    for(uint64_t tag:{3ULL,5ULL})for(uint64_t value:{0ULL,1ULL,0xffffffffULL})assert(snapshot::integer((tag<<58)|(value<<1))==value);
    for(uint64_t bad:{uint64_t(3)<<58|1,(uint64_t(3)<<58)|(uint64_t(1)<<33),uint64_t(4)<<58,(uint64_t(3)<<58)|(uint64_t(1)<<63)})rejects([&]{snapshot::integer(bad);});
    assert(snapshot::uid_address(0xa000000012345678ULL)==0x12345678);rejects([&]{snapshot::uid_address(0xb000000012345678ULL);});
    const uint8_t records[]={0x55,0x9c,0,0,0x78,0x56,0x34,0x12,0,0,0,0xa0};
    assert(snapshot::field(records,0x9c55)==0xa000000012345678ULL);rejects([&]{snapshot::field(records,0x9c45);});rejects([&]{snapshot::field(std::span(records).first(11),0x9c55);});
    const uint8_t example[]={0xac,0x02};auto v=wire::decode(example);assert(v.value==300&&v.consumed==2);
    const uint8_t maximum[]={255,255,255,255,255,255,255,255,255,1};v=wire::decode(maximum);assert(v.value==UINT64_MAX&&v.consumed==10);
    const uint8_t noncanonical[]={0x80,0};v=wire::decode(noncanonical);assert(v.value==0&&v.consumed==2);
    const uint8_t offset[]={42,0xac,2};v=wire::decode(offset,1);assert(v.value==300&&v.consumed==2);
    for(int kind=0;kind<3;kind++){bool caught=false;try{
        if(kind==0)wire::decode(std::span<const uint8_t>{});
        if(kind==1)wire::decode(std::span(maximum).first(9));
        if(kind==2){auto bad=std::vector<uint8_t>(10,255);wire::decode(bad);}
    }catch(const std::exception&){caught=true;}assert(caught);}
    for(int i=0;i<1000;i++){
        pipe::Frame f;f.op=uint16_t(random());f.request=random();f.flags=random();f.status=int32_t(random());f.value=(uint64_t(random())<<32)|random();f.command="MessageSvc.Test";f.message="中文 test";f.body.resize(random()%2048);for(auto& b:f.body)b=uint8_t(random());
        auto wire=pipe::encode(f);pipe::Reader reader;std::vector<pipe::Frame> decoded;size_t at=0;
        while(at<wire.size()){size_t n=std::min<size_t>(1+random()%61,wire.size()-at);auto items=reader.push(std::span(wire).subspan(at,n));for(auto& x:items)decoded.push_back(std::move(x));at+=n;}
        assert(decoded.size()==1);assert(pipe::encode(decoded[0])==wire);assert(reader.buffered()==0);
    }
    auto bad=pipe::encode({});bad[0]=0;pipe::Reader reader;bool rejected=false;try{reader.push(bad);}catch(...){rejected=true;}assert(rejected);
    bad=pipe::encode({});bad[23]=1;pipe::Reader excessive;rejected=false;try{excessive.push(bad);}catch(...){rejected=true;}assert(rejected);
    std::vector<uint8_t> invalid(64);rejected=false;try{PeImage image(invalid);}catch(...){rejected=true;}assert(rejected);
    if(argc>1){auto image=PeImage::read(argv[1]);assert(image.export_rva("snowluma_prepare_unload_v1"));assert(!image.export_rva("absent"));image.require_manual_map_supported();image.relocate(image.nt().OptionalHeader.ImageBase+0x1000000);}
    if(argc>2){
        auto hook=PeImage::read(argv[2]);assert(hook.export_rva("snowluma_prepare_unload_v1")==0x1000);
        auto tls=hook.directory(IMAGE_DIRECTORY_ENTRY_TLS);auto table=hook.get<IMAGE_TLS_DIRECTORY64>(tls.VirtualAddress);
        assert(table->EndAddressOfRawData-table->StartAddressOfRawData==8);
        rejects([&]{hook.require_manual_map_supported();});
        hook.nt().OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_TLS].Size=1;rejects([&]{hook.validate_dependencies();});
        hook.nt().OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_TLS]=tls;
        auto malformed=*table;malformed.AddressOfIndex=hook.nt().OptionalHeader.ImageBase-1;
        memcpy(hook.bytes.data()+tls.VirtualAddress,&malformed,sizeof(malformed));rejects([&]{hook.validate_dependencies();});
    }
    std::cout<<"1000 framed-stream cases, 7 varint cases, snapshot decoding and TLS inspection regressions passed\n";
}

#include "remote_stub.hpp"
// Compiled in dedicated ordered sections; only indirect calls through the context are allowed.
// No copied bytes from the original DLL, assembly blobs, or original-binary fallback.
#pragma code_seg(push,".stub$a")
namespace recovery {
__declspec(noinline) DWORD WINAPI map_stub(StubContext* c){
    auto image=reinterpret_cast<uint8_t*>(c->image);
    auto main=reinterpret_cast<BOOL (WINAPI*)(HINSTANCE,DWORD,LPVOID)>(c->entry);
    if(c->detach){
        if(main)main(reinterpret_cast<HINSTANCE>(image),DLL_PROCESS_DETACH,nullptr);
        if(c->exceptionTable)reinterpret_cast<BOOLEAN(WINAPI*)(PRUNTIME_FUNCTION)>(c->deleteTable)(reinterpret_cast<PRUNTIME_FUNCTION>(c->exceptionTable));
        c->status=1;return 0;
    }
    auto dos=reinterpret_cast<IMAGE_DOS_HEADER*>(image);
    auto nt=reinterpret_cast<IMAGE_NT_HEADERS64*>(image+dos->e_lfanew);
    auto imports=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    c->status=2;
    if(imports.Size){
        auto desc=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(image+imports.VirtualAddress);
        for(;desc->Name;desc++){
            HMODULE module=reinterpret_cast<HMODULE(WINAPI*)(LPCSTR)>(c->getModule)(reinterpret_cast<char*>(image+desc->Name));
            if(!module){c->status=101;return 101;}
            auto source=reinterpret_cast<IMAGE_THUNK_DATA64*>(image+(desc->OriginalFirstThunk?desc->OriginalFirstThunk:desc->FirstThunk));
            auto dest=reinterpret_cast<IMAGE_THUNK_DATA64*>(image+desc->FirstThunk);
            for(;source->u1.AddressOfData;source++,dest++){
                LPCSTR name=(source->u1.Ordinal&IMAGE_ORDINAL_FLAG64)?reinterpret_cast<LPCSTR>(source->u1.Ordinal&0xffff):reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(image+source->u1.AddressOfData)->Name;
                auto proc=reinterpret_cast<FARPROC(WINAPI*)(HMODULE,LPCSTR)>(c->getProc)(module,name);
                if(!proc){c->status=102;return 102;}dest->u1.Function=reinterpret_cast<uint64_t>(proc);
            }
        }
    }
    c->status=3;
    auto section=reinterpret_cast<IMAGE_SECTION_HEADER*>(reinterpret_cast<uint8_t*>(&nt->OptionalHeader)+nt->FileHeader.SizeOfOptionalHeader);
    for(unsigned i=0;i<nt->FileHeader.NumberOfSections;i++){
        DWORD flags=section[i].Characteristics;
        DWORD protect=(flags&IMAGE_SCN_MEM_EXECUTE)?((flags&IMAGE_SCN_MEM_WRITE)?PAGE_EXECUTE_READWRITE:((flags&IMAGE_SCN_MEM_READ)?PAGE_EXECUTE_READ:PAGE_EXECUTE)):((flags&IMAGE_SCN_MEM_WRITE)?PAGE_READWRITE:((flags&IMAGE_SCN_MEM_READ)?PAGE_READONLY:PAGE_NOACCESS));
        DWORD old=0;DWORD length=section[i].Misc.VirtualSize>section[i].SizeOfRawData?section[i].Misc.VirtualSize:section[i].SizeOfRawData;
        if(length&&!reinterpret_cast<BOOL(WINAPI*)(LPVOID,SIZE_T,DWORD,PDWORD)>(c->protect)(image+section[i].VirtualAddress,length,protect,&old)){c->status=103;return 103;}
    }
    DWORD old=0;reinterpret_cast<BOOL(WINAPI*)(LPVOID,SIZE_T,DWORD,PDWORD)>(c->protect)(image,nt->OptionalHeader.SizeOfHeaders,PAGE_READONLY,&old);
    reinterpret_cast<BOOL(WINAPI*)(HANDLE,LPCVOID,SIZE_T)>(c->flush)(reinterpret_cast<HANDLE(WINAPI*)()>(c->currentProcess)(),image,nt->OptionalHeader.SizeOfImage);
    c->status=4;
    if(c->exceptionTable&&!reinterpret_cast<BOOLEAN(WINAPI*)(PRUNTIME_FUNCTION,DWORD,DWORD64)>(c->addTable)(reinterpret_cast<PRUNTIME_FUNCTION>(c->exceptionTable),c->exceptionCount,c->image)){c->status=104;return 104;}
    c->status=5;
    auto tls=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_TLS];
    if(tls.Size){auto table=reinterpret_cast<IMAGE_TLS_DIRECTORY64*>(image+tls.VirtualAddress);auto cb=reinterpret_cast<PIMAGE_TLS_CALLBACK*>(table->AddressOfCallBacks);if(cb)for(;*cb;cb++)(*cb)(image,DLL_PROCESS_ATTACH,nullptr);}
    c->status=6;
    if(main&&!main(reinterpret_cast<HINSTANCE>(image),DLL_PROCESS_ATTACH,nullptr)){
        if(c->exceptionTable)reinterpret_cast<BOOLEAN(WINAPI*)(PRUNTIME_FUNCTION)>(c->deleteTable)(reinterpret_cast<PRUNTIME_FUNCTION>(c->exceptionTable));
        c->status=106;return 106;
    }
    c->status=1;return 0;
}
}
#pragma code_seg(pop)
#pragma code_seg(push,".stub$b")
namespace recovery {__declspec(noinline) void map_stub_end(){}}
#pragma code_seg(pop)

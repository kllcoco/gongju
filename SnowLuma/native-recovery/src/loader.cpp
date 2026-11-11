#include "native.hpp"
#include "pe_image.hpp"
#include "remote_stub.hpp"
#include <tlhelp32.h>
#include <filesystem>
#include <mutex>
#include <algorithm>
#include <memory>
namespace recovery {
namespace {
constexpr DWORD rights=PROCESS_CREATE_THREAD|PROCESS_QUERY_INFORMATION|PROCESS_VM_OPERATION|PROCESS_VM_WRITE|PROCESS_VM_READ;
constexpr DWORD waitMilliseconds=15000;
std::mutex loading;
struct Module {uint64_t base{};std::wstring path;};
Module module_named(DWORD pid,const std::wstring& name){
    for(int attempt=0;attempt<8;attempt++){
        Handle modules(CreateToolhelp32Snapshot(TH32CS_SNAPMODULE|TH32CS_SNAPMODULE32,pid));
        if(!modules){if(GetLastError()==ERROR_BAD_LENGTH){Sleep(10);continue;}win_error("CreateToolhelp32Snapshot(modules)");}
        MODULEENTRY32W m{};m.dwSize=sizeof(m);
        bool fullPath=std::filesystem::path(name).has_parent_path();
        for(BOOL ok=Module32FirstW(modules.value,&m);ok;ok=Module32NextW(modules.value,&m))if(_wcsicmp(fullPath?m.szExePath:m.szModule,name.c_str())==0)return {reinterpret_cast<uint64_t>(m.modBaseAddr),m.szExePath};
        return {};
    }throw LoadError("Module snapshot kept changing");
}
uint64_t remote_function(DWORD pid,FARPROC function){
    HMODULE module{};if(!function||!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,reinterpret_cast<LPCWSTR>(function),&module))win_error("GetModuleHandleExW");
    WCHAR name[32768]{};if(!GetModuleFileNameW(module,name,32768))win_error("GetModuleFileNameW");
    auto remote=module_named(pid,std::filesystem::path(name).filename().wstring());if(!remote.base)throw LoadError("Required system module missing in target");
    return remote.base+reinterpret_cast<uint64_t>(function)-reinterpret_cast<uint64_t>(module);
}
Handle* process(DWORD pid){
    if(!pid)throw LoadError("Bad pid");auto h=new Handle(OpenProcess(rights,FALSE,pid));
    if(!*h){delete h;win_error("OpenProcess");}
    BOOL targetWow=FALSE,selfWow=FALSE;
    if(!IsWow64Process(h->value,&targetWow)||!IsWow64Process(GetCurrentProcess(),&selfWow)||targetWow!=selfWow){delete h;throw LoadError("Architecture mismatch");}
    return h;
}
struct Remote {
    HANDLE process{};void* pointer{};bool retained{};
    Remote(HANDLE p,SIZE_T size):process(p),pointer(VirtualAllocEx(p,nullptr,size,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE)){if(!pointer)win_error("VirtualAllocEx");}
    ~Remote(){if(pointer&&!retained)VirtualFreeEx(process,pointer,0,MEM_RELEASE);}
    void write(const void* data,SIZE_T length,SIZE_T offset=0){SIZE_T n=0;if(!WriteProcessMemory(process,static_cast<uint8_t*>(pointer)+offset,data,length,&n)||n!=length)win_error("WriteProcessMemory");}
};
DWORD execute(HANDLE p,uint64_t address,void* argument,bool& timedOut){
    Handle thread(CreateRemoteThread(p,nullptr,0,reinterpret_cast<LPTHREAD_START_ROUTINE>(address),argument,0,nullptr));if(!thread)win_error("CreateRemoteThread");
    DWORD wait=WaitForSingleObject(thread.value,waitMilliseconds);
    // A timed-out thread may still use the image/stub. Never free its memory or terminate it blindly.
    if(wait!=WAIT_OBJECT_0){timedOut=true;throw LoadError("Remote operation did not finish; image retained",wait==WAIT_TIMEOUT);}
    DWORD exit=0;if(!GetExitCodeThread(thread.value,&exit))win_error("GetExitCodeThread");return exit;
}
void prepare(HANDLE p,uint64_t address){if(!address)return;bool timed=false;DWORD code=execute(p,address,nullptr,timed);if(code)throw LoadError("prepare unload refused; image retained",code==ERROR_TIMEOUT);}
void run_stub(HANDLE p,StubContext& context,bool& imageRetained){
    auto start=reinterpret_cast<uintptr_t>(&map_stub),end=reinterpret_cast<uintptr_t>(&map_stub_end);
    if(end<=start||end-start>32768)throw LoadError("Invalid compiled stub range");
    size_t length=end-start,aligned=(length+4095)&~size_t(4095);Remote memory(p,aligned+sizeof(context));
    memory.write(reinterpret_cast<const void*>(start),length);memory.write(&context,sizeof(context),aligned);
    DWORD old=0;if(!VirtualProtectEx(p,memory.pointer,aligned,PAGE_EXECUTE_READ,&old))win_error("VirtualProtectEx(stub)");
    if(!FlushInstructionCache(p,memory.pointer,length))win_error("FlushInstructionCache(stub)");
    try {DWORD code=execute(p,reinterpret_cast<uint64_t>(memory.pointer),static_cast<uint8_t*>(memory.pointer)+aligned,memory.retained);
        SIZE_T n=0;if(!ReadProcessMemory(p,static_cast<uint8_t*>(memory.pointer)+aligned,&context,sizeof(context),&n)||n!=sizeof(context))win_error("ReadProcessMemory(stub)");
        if(code||context.status!=1){if(context.status>=4&&context.status<100)imageRetained=true;throw LoadError("Manual map stub failed at stage "+std::to_string(context.status));}
    }catch(...){
        // An incomplete context read cannot establish whether callbacks ran.
        // Conservatively retain the image on every remote-stage failure.
        imageRetained=true;throw;
    }
}
StubContext helpers(DWORD pid){
    StubContext c;auto kernel=GetModuleHandleW(L"kernel32.dll"),ntdll=GetModuleHandleW(L"ntdll.dll");
    c.getModule=remote_function(pid,GetProcAddress(kernel,"GetModuleHandleA"));c.getProc=remote_function(pid,GetProcAddress(kernel,"GetProcAddress"));
    c.addTable=remote_function(pid,GetProcAddress(ntdll,"RtlAddFunctionTable"));c.deleteTable=remote_function(pid,GetProcAddress(ntdll,"RtlDeleteFunctionTable"));
    c.protect=remote_function(pid,GetProcAddress(kernel,"VirtualProtect"));c.flush=remote_function(pid,GetProcAddress(kernel,"FlushInstructionCache"));c.currentProcess=remote_function(pid,GetProcAddress(kernel,"GetCurrentProcess"));return c;
}
}
uint64_t normal_load(DWORD pid,const std::wstring& path){
    std::lock_guard guard(loading);if(path.empty()||path.find(L'\0')!=std::wstring::npos)throw LoadError("dll path is empty or contains NUL");
    std::unique_ptr<Handle> p(process(pid));std::wstring absolute=std::filesystem::absolute(path).wstring();Remote data(p->value,(absolute.size()+1)*2);data.write(absolute.c_str(),(absolute.size()+1)*2);
    DWORD result=execute(p->value,remote_function(pid,GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"LoadLibraryW")),data.pointer,data.retained);
    if(!result&&!module_named(pid,std::filesystem::path(absolute).filename().wstring()).base)throw LoadError("LoadLibrary failed in target");return result;
}
void normal_unload(DWORD pid,const std::wstring& path){
    std::lock_guard guard(loading);if(path.empty()||path.find(L'\0')!=std::wstring::npos)throw LoadError("dll path is empty or contains NUL");
    std::unique_ptr<Handle> p(process(pid));auto module=module_named(pid,std::filesystem::absolute(path).lexically_normal().wstring());if(!module.base)throw LoadError("Module not found in target process");
    auto image=PeImage::read(module.path);uint32_t rva=image.export_rva("snowluma_prepare_unload_v1");prepare(p->value,rva?module.base+rva:0);
    bool timed=false;DWORD result=execute(p->value,remote_function(pid,GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"FreeLibrary")),reinterpret_cast<void*>(module.base),timed);
    if(!result)throw LoadError("FreeLibrary failed");
}
ManualHandle manual_load(DWORD pid,const std::wstring& path){
    std::lock_guard guard(loading);auto image=PeImage::read(path);image.require_manual_map_supported();std::unique_ptr<Handle> p(process(pid));Remote allocation(p->value,image.bytes.size());
    uint64_t base=reinterpret_cast<uint64_t>(allocation.pointer);ManualHandle result;result.base=base;result.size=uint32_t(image.bytes.size());
    result.entry=image.nt().OptionalHeader.AddressOfEntryPoint?base+image.nt().OptionalHeader.AddressOfEntryPoint:0;auto prep=image.export_rva("snowluma_prepare_unload_v1");result.prepare=prep?base+prep:0;
    auto exceptions=image.directory(IMAGE_DIRECTORY_ENTRY_EXCEPTION);result.exceptionTable=exceptions.Size?base+exceptions.VirtualAddress:0;
    image.relocate(base);allocation.write(image.bytes.data(),image.bytes.size());auto c=helpers(pid);c.image=base;c.entry=result.entry;c.exceptionTable=result.exceptionTable;c.exceptionCount=exceptions.Size/sizeof(RUNTIME_FUNCTION);
    run_stub(p->value,c,allocation.retained);allocation.retained=true;return result;
}
void manual_unload(DWORD pid,const ManualHandle& h){
    std::lock_guard guard(loading);if(!h.base||!h.size)throw LoadError("Invalid manual handle");
    auto in=[&](uint64_t value){return !value||(value>=h.base&&value-h.base<h.size);};
    if(!in(h.entry)||!in(h.prepare)||!in(h.exceptionTable))throw LoadError("Handle address outside image");
    std::unique_ptr<Handle> p(process(pid));MEMORY_BASIC_INFORMATION mbi{};
    if(!VirtualQueryEx(p->value,reinterpret_cast<void*>(h.base),&mbi,sizeof(mbi))||reinterpret_cast<uint64_t>(mbi.AllocationBase)!=h.base||mbi.State!=MEM_COMMIT||mbi.Type!=MEM_PRIVATE)throw LoadError("Manual image is no longer mapped");
    prepare(p->value,h.prepare);auto c=helpers(pid);c.image=h.base;c.entry=h.entry;c.exceptionTable=h.exceptionTable;c.detach=1;bool retained=false;run_stub(p->value,c,retained);
    if(!VirtualFreeEx(p->value,reinterpret_cast<void*>(h.base),0,MEM_RELEASE))win_error("VirtualFreeEx(image)");
}
}

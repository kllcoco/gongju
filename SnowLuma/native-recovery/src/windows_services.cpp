#include "native.hpp"
#include <tlhelp32.h>
#include <restartmanager.h>
#include <aclapi.h>
namespace recovery {
std::string utf8(const std::wstring& s){if(s.empty())return {};int n=WideCharToMultiByte(CP_UTF8,0,s.data(),int(s.size()),nullptr,0,nullptr,nullptr);std::string v(n,'\0');WideCharToMultiByte(CP_UTF8,0,s.data(),int(s.size()),v.data(),n,nullptr,nullptr);return v;}
void win_error(const char* operation,DWORD code){throw std::runtime_error(std::string(operation)+" failed ("+std::to_string(code)+")");}
// Original function RVA 0x1230: QQ.exe AND a loaded wrapper.node; not just the process name.
std::vector<DWORD> main_processes(){
    Handle snapshot(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0));
    if(!snapshot)throw std::runtime_error("CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS) failed");
    std::vector<DWORD> result;PROCESSENTRY32W p{};p.dwSize=sizeof(p);
    for(BOOL ok=Process32FirstW(snapshot.value,&p);ok;ok=Process32NextW(snapshot.value,&p)){
        if(_wcsicmp(p.szExeFile,L"QQ.exe")!=0)continue;
        Handle modules(CreateToolhelp32Snapshot(TH32CS_SNAPMODULE|TH32CS_SNAPMODULE32,p.th32ProcessID));
        if(!modules)continue;MODULEENTRY32W m{};m.dwSize=sizeof(m);
        for(BOOL found=Module32FirstW(modules.value,&m);found;found=Module32NextW(modules.value,&m))
            if(_wcsicmp(m.szModule,L"wrapper.node")==0){result.push_back(p.th32ProcessID);break;}
    }return result;
}
// Original RVA 0x8630: missing file -> []; directory -> error; four Restart Manager attempts.
std::vector<FileHolder> file_holders(const std::wstring& path){
    DWORD attrs=GetFileAttributesW(path.c_str());
    if(attrs==INVALID_FILE_ATTRIBUTES){DWORD e=GetLastError();if(e==ERROR_FILE_NOT_FOUND||e==ERROR_PATH_NOT_FOUND)return {};win_error("GetFileAttributesW",e);}
    if(attrs&FILE_ATTRIBUTE_DIRECTORY)throw std::runtime_error("path must name a file");
    DWORD session=0;WCHAR key[CCH_RM_SESSION_KEY+1]{};DWORD e=RmStartSession(&session,0,key);if(e)win_error("RmStartSession",e);
    struct End {DWORD id;~End(){RmEndSession(id);}} end{session};
    LPCWSTR resource=path.c_str();e=RmRegisterResources(session,1,&resource,0,nullptr,0,nullptr);if(e)win_error("RmRegisterResources",e);
    std::vector<RM_PROCESS_INFO> list;
    for(int attempt=0;attempt<4;attempt++){
        UINT needed=0,count=UINT(list.size());DWORD reasons=0;
        e=RmGetList(session,&needed,&count,list.empty()?nullptr:list.data(),&reasons);
        if(!e){std::vector<FileHolder> result;for(UINT i=0;i<count;i++)result.push_back({list[i].Process.dwProcessId,list[i].strAppName});return result;}
        if(e!=ERROR_MORE_DATA)win_error("RmGetList",e);list.resize(needed);
    }throw std::runtime_error("file holders kept changing during RmGetList");
}
Integrity runtime_integrity(){
    Integrity out;out.pid=GetCurrentProcessId();std::vector<WCHAR> path(32768);
    DWORD len=GetModuleFileNameW(nullptr,path.data(),DWORD(path.size()));if(len&&len<path.size())out.path.assign(path.data(),len);
    HANDLE raw{};
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&raw))out.processError={"OpenProcessToken",GetLastError()};
    else {
        Handle token(raw);DWORD size=0;GetTokenInformation(raw,TokenIntegrityLevel,nullptr,0,&size);std::vector<uint8_t> buffer(size);
        if(!size||!GetTokenInformation(raw,TokenIntegrityLevel,buffer.data(),size,&size))out.processError={"GetTokenInformation",GetLastError()};
        else {auto label=reinterpret_cast<TOKEN_MANDATORY_LABEL*>(buffer.data());PSID sid=label->Label.Sid;
            if(!IsValidSid(sid)||!*GetSidSubAuthorityCount(sid))out.processError={"TokenIntegrityLevel.Sid",ERROR_INVALID_SID};
            else out.processRid=*GetSidSubAuthority(sid,*GetSidSubAuthorityCount(sid)-1);}
    }
    if(out.path.empty())out.fileError={"GetModuleFileNameW",GetLastError()};
    else {
        PACL sacl=nullptr;PSECURITY_DESCRIPTOR descriptor=nullptr;
        DWORD e=GetNamedSecurityInfoW(out.path.data(),SE_FILE_OBJECT,LABEL_SECURITY_INFORMATION,nullptr,nullptr,nullptr,&sacl,&descriptor);
        struct Free {void* p;~Free(){if(p)LocalFree(p);}} free{descriptor};
        if(e)out.fileError={"GetNamedSecurityInfoW",e};
        else if(sacl)for(DWORD i=0;i<sacl->AceCount;i++){
            void* ptr=nullptr;if(!GetAce(sacl,i,&ptr)){out.fileError={"GetAce",GetLastError()};break;}
            auto ace=static_cast<SYSTEM_MANDATORY_LABEL_ACE*>(ptr);if(ace->Header.AceType!=SYSTEM_MANDATORY_LABEL_ACE_TYPE)continue;
            PSID sid=&ace->SidStart;if(!IsValidSid(sid)||!*GetSidSubAuthorityCount(sid)){out.fileError={"MandatoryLabel.Sid",ERROR_INVALID_SID};break;}
            out.fileLabel=true;out.fileRid=*GetSidSubAuthority(sid,*GetSidSubAuthorityCount(sid)-1);out.filePolicy=ace->Mask;out.inherited=(ace->Header.AceFlags&INHERITED_ACE)!=0;break;
        }
    }return out;
}
}

#include <windows.h>
#include <iostream>
#include <string>
#include <sstream>
int wmain(int argc,wchar_t** argv){
    if(argc<2)return 2;std::wstring prefix=argv[1];SetEnvironmentVariableW(L"SNOWLUMA_RECOVERY_EVENT_PREFIX",prefix.c_str());
    HANDLE events[3];const wchar_t* names[]={L"Attach",L"Prepare",L"Detach"};
    for(int i=0;i<3;i++){events[i]=CreateEventW(nullptr,TRUE,FALSE,(prefix+names[i]).c_str());if(!events[i])return 3;}
    HMODULE preloaded=nullptr;if(argc>2){preloaded=LoadLibraryW(argv[2]);if(!preloaded)return 4;}
    std::cout<<"READY "<<GetCurrentProcessId()<<std::endl;
    for(std::string line;std::getline(std::cin,line);){
        if(line=="quit")break;
        if(line=="events"){int bits=0;for(int i=0;i<3;i++)if(WaitForSingleObject(events[i],0)==WAIT_OBJECT_0)bits|=1<<i;std::cout<<bits<<std::endl;}
        else if(line=="reset"){for(auto h:events)ResetEvent(h);std::cout<<"ok"<<std::endl;}
        else if(line.rfind("probe ",0)==0){uint64_t address=std::stoull(line.substr(6),nullptr,16);MEMORY_BASIC_INFORMATION info{};VirtualQuery(reinterpret_cast<void*>(address),&info,sizeof(info));std::cout<<(info.State==MEM_COMMIT?"mapped":"free")<<std::endl;}
        else std::cout<<"unknown"<<std::endl;
    }
    if(preloaded)FreeLibrary(preloaded);for(auto h:events)CloseHandle(h);return 0;
}

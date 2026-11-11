#include <windows.h>
#include <cwchar>
static void event(const wchar_t* suffix){WCHAR name[240]{};if(!GetEnvironmentVariableW(L"SNOWLUMA_RECOVERY_EVENT_PREFIX",name,200))return;wcscat_s(name,suffix);HANDLE h=OpenEventW(EVENT_MODIFY_STATE,FALSE,name);if(h){SetEvent(h);CloseHandle(h);}}
extern "C" __declspec(dllexport) DWORD snowluma_prepare_unload_v1(){event(L"Prepare");
#ifdef FIXTURE_BUSY
return ERROR_BUSY;
#else
return ERROR_SUCCESS;
#endif
}
BOOL WINAPI DllMain(HINSTANCE,DWORD reason,LPVOID){if(reason==DLL_PROCESS_ATTACH)event(L"Attach");if(reason==DLL_PROCESS_DETACH)event(L"Detach");return TRUE;}

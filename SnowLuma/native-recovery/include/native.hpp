#pragma once
#include <windows.h>
#include <cstdint>
#include <string>
#include <vector>
#include <stdexcept>
namespace recovery {
struct Handle {
    HANDLE value{};
    explicit Handle(HANDLE h=nullptr):value(h){}
    ~Handle(){if(value && value!=INVALID_HANDLE_VALUE)CloseHandle(value);}
    Handle(const Handle&)=delete;Handle& operator=(const Handle&)=delete;
    explicit operator bool() const{return value && value!=INVALID_HANDLE_VALUE;}
};
struct ReadError {std::string operation; DWORD code{};};
struct Integrity { DWORD pid{},processRid{},fileRid{},filePolicy{}; bool fileLabel{},inherited{}; std::wstring path; ReadError processError,fileError; };
struct FileHolder {DWORD pid; std::wstring name;};
// Original ABI has FIVE fields; the upstream TS interface omitted prepare.
struct ManualHandle {uint64_t base{},entry{},exceptionTable{};uint32_t size{};uint64_t prepare{};};
static_assert(sizeof(ManualHandle)==40);
struct LoadError:std::runtime_error {bool timeout; explicit LoadError(std::string message,bool timed=false):runtime_error(message),timeout(timed){}};
std::vector<DWORD> main_processes();
Integrity runtime_integrity();
std::vector<FileHolder> file_holders(const std::wstring&);
uint64_t normal_load(DWORD,const std::wstring&);
void normal_unload(DWORD,const std::wstring&);
ManualHandle manual_load(DWORD,const std::wstring&);
void manual_unload(DWORD,const ManualHandle&);
std::string utf8(const std::wstring&);
[[noreturn]] void win_error(const char* operation,DWORD code=GetLastError());
}

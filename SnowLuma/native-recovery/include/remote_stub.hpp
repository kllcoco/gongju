#pragma once
#include "native.hpp"
namespace recovery {
struct StubContext {
    uint64_t image{},entry{},exceptionTable{},getModule{},getProc{},addTable{},deleteTable{},protect{},flush{},currentProcess{};
    uint32_t exceptionCount{},status{},detach{};
};
DWORD WINAPI map_stub(StubContext*);
void map_stub_end();
}

#pragma once
#include <windows.h>
#include <cstdint>
#include <cstddef>
namespace recovery::resident {
using Word=uintptr_t;
using GateFn=Word(*)(Word,Word,Word);
struct Closure;
using Manager=void*(*)(Word*,int,Word*,Word,void*,size_t);
using Invoke=void(*)(Closure*,Word,uint32_t,Word,Word);
struct Closure {Word state{},reserved{};Manager manager{};Invoke invoke{};Word tail{};};
using Callback=void(*)(void*,uint32_t,Word,Word);
using Destroy=void(*)(void*);
struct Api {
    uint32_t size,reserved;
    GateFn (*bind)(uint32_t,GateFn,GateFn);
    Word* (*cloneVtable)(const Word*,size_t);
    void (*quiesce)();
    uint64_t (*active)();
    void (*empty)(Closure*);
    Word (*makeClosure)(void*,Callback,Destroy,Closure*);
};
static_assert(sizeof(Api)==0x38);static_assert(sizeof(Closure)==0x28);
}

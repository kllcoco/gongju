#include "resident.hpp"
#include <atomic>
#include <thread>
#include <iostream>
#include <stdexcept>
using namespace recovery::resident;
#define CHECK(x) do{if(!(x))throw std::runtime_error("failed: " #x);}while(0)
static const Api* api;
static std::atomic<bool> entered{},proceed{};
Word fallback(Word a,Word b,Word c){return 10+a+b+c;}
Word hook(Word a,Word b,Word c){CHECK(api->active()>=1);return 100+a+b+c;}
Word blocking(Word a,Word b,Word c){entered=true;while(!proceed.load())Sleep(1);return 1000+a+b+c;}
struct Context {std::atomic<int> calls{},destroyed{};std::atomic<bool> entered{},proceed{};bool block{};Word total{};};
void callback(void* p,uint32_t status,Word a,Word b){auto& c=*static_cast<Context*>(p);c.calls++;c.total=status+a+b;CHECK(api->active()>=1);c.entered=true;while(c.block&&!c.proceed.load())Sleep(1);}
void destroy(void* p){static_cast<Context*>(p)->destroyed++;CHECK(api->active()>=1);}
void throwing_callback(void*,uint32_t,Word,Word){CHECK(api->active()>=1);throw std::runtime_error("callback failure");}
void throwing_destroy(void* p){destroy(p);throw std::runtime_error("cleanup failure");}
Word throwing_hook(Word,Word,Word){throw std::runtime_error("hook failure");}
void dispose(Closure& c){c.manager(reinterpret_cast<Word*>(&c.manager),2,nullptr,0,nullptr,0);}
void await(std::atomic<bool>& flag){for(int i=0;i<5000&&!flag.load();i++)Sleep(1);CHECK(flag.load());}
int wmain(int argc,wchar_t** argv){try{
    CHECK(argc==2||argc==3);auto dll=LoadLibraryW(argv[1]);CHECK(dll);auto getter=reinterpret_cast<const Api*(*)()>(GetProcAddress(dll,"nnp_resident_dispatch_win"));CHECK(getter);api=getter();CHECK(api&&api->size==56&&api->reserved==0);
    if(argc==3){
        CHECK(wcscmp(argv[2],L"--exceptions")==0);
        auto throws=[](auto fn){bool caught=false;try{fn();}catch(const std::runtime_error&){caught=true;}CHECK(caught);};
        Context a,b,c;Closure x{},y{},z{};
        CHECK(api->makeClosure(&a,throwing_callback,destroy,&x));throws([&]{x.invoke(&x,0,0,0,0);});CHECK(api->active()==0);
        auto gate=api->bind(0,fallback,throwing_hook);CHECK(gate);throws([&]{gate(0,0,0);});CHECK(api->active()==0);
        CHECK(api->makeClosure(&b,callback,throwing_destroy,&y));CHECK(api->makeClosure(&c,callback,destroy,&z));
        throws([&]{api->quiesce();});CHECK(a.destroyed==1&&b.destroyed==1&&c.destroyed==1&&api->active()==0);
        dispose(x);dispose(y);dispose(z);api->quiesce();CHECK(api->active()==0);FreeLibrary(dll);
        std::cout<<"exception cleanup and active counters passed\n";return 0;
    }
    CHECK(!api->bind(4,fallback,hook)&&GetLastError()==ERROR_INVALID_PARAMETER);
    auto gate=api->bind(0,fallback,hook);CHECK(gate);CHECK(gate(2,3,4)==109);CHECK(api->active()==0);CHECK(api->bind(0,fallback,hook)==gate);
    CHECK(!api->bind(0,fallback,fallback)&&GetLastError()==ERROR_ALREADY_EXISTS);
    Word table[]={0x11223344,0xaabbccdd,0x778899};auto copied=api->cloneVtable(table,3);CHECK(copied&&copied[-1]==table[0]&&copied[1]==table[2]);MEMORY_BASIC_INFORMATION info{};CHECK(VirtualQuery(copied,&info,sizeof(info)));CHECK(info.Protect==PAGE_READONLY);
    CHECK(!api->cloneVtable(table,1)&&GetLastError()==ERROR_INVALID_PARAMETER);
    Closure empty{};api->empty(&empty);CHECK(!empty.state&&empty.manager&&empty.invoke);Word isEmpty=9;empty.manager(reinterpret_cast<Word*>(&empty.manager),4,nullptr,0,&isEmpty,sizeof(isEmpty));CHECK(isEmpty==1);
    Context context;Closure first{},copy{},moved{};CHECK(api->makeClosure(&context,callback,destroy,&first));first.invoke(&first,0,3,4,5);CHECK(context.calls==1&&context.total==12);
    isEmpty=9;first.manager(reinterpret_cast<Word*>(&first.manager),4,nullptr,0,&isEmpty,sizeof(isEmpty));CHECK(isEmpty==0);
    first.manager(reinterpret_cast<Word*>(&copy.manager),1,reinterpret_cast<Word*>(&first),0,&copy,16);CHECK(copy.state==first.state);dispose(first);CHECK(context.destroyed==0);copy.invoke(&copy,0,4,5,6);CHECK(context.calls==2);
    copy.manager(reinterpret_cast<Word*>(&moved.manager),0,reinterpret_cast<Word*>(&copy),0,&moved,16);CHECK(!copy.state&&moved.state);dispose(copy);CHECK(context.destroyed==0);
    Context busy;busy.block=true;Closure active{};CHECK(api->makeClosure(&busy,callback,destroy,&active));auto gate2=api->bind(1,fallback,blocking);CHECK(gate2);
    std::thread a([&]{CHECK(gate2(1,2,3)==1006);});std::thread b([&]{active.invoke(&active,0,1,2,3);});await(entered);await(busy.entered);CHECK(api->active()==2);
    api->quiesce();CHECK(context.destroyed==1);CHECK(busy.destroyed==0);CHECK(gate(1,2,3)==16);CHECK(api->active()==2);
    moved.invoke(&moved,0,9,9,9);CHECK(context.calls==2);CHECK(!api->bind(2,fallback,hook)&&GetLastError()==ERROR_SHUTDOWN_IN_PROGRESS);
    CHECK(!api->makeClosure(&context,callback,destroy,&empty)&&GetLastError()==ERROR_SHUTDOWN_IN_PROGRESS);
    proceed=true;busy.proceed=true;a.join();b.join();CHECK(busy.destroyed==1&&api->active()==0);
    dispose(moved);dispose(active);CHECK(context.destroyed==1&&busy.destroyed==1);api->quiesce();CHECK(api->active()==0);
    VirtualFree(copied-1,0,MEM_RELEASE);FreeLibrary(dll);std::cout<<"resident ABI, copy/move, fallback, and concurrent quiescence passed\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}}

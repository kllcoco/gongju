#include "resident.hpp"
#include <atomic>
#include <cstring>
#include <new>
#include <exception>
using namespace recovery::resident;
namespace {
SRWLOCK lock=SRWLOCK_INIT;
bool stopped=false;
struct Gate {std::atomic<uint64_t> state{};GateFn fallback{},hook{};} gates[4];
constexpr uint64_t closed=uint64_t(1)<<63;
std::atomic<uint64_t> extra{};
struct Node {uint64_t references{1},active{};void* context{};Callback callback{};Destroy destroy{};Node* previous{},*next{},*pendingNext{};void* pendingContext{};Callback pendingCallback{};Destroy pendingDestroy{};};
static_assert(sizeof(Node)==0x58);
Node* head{};
struct ExtraGuard {~ExtraGuard(){extra.fetch_sub(1);}};
template<unsigned slot>Word dispatch(Word a,Word b,Word c){
    auto& gate=gates[slot];uint64_t value=gate.state.load();
    for(;;){if(value&closed)return gate.fallback(a,b,c);if(gate.state.compare_exchange_weak(value,value+1))break;}
    struct Exit {Gate& gate;~Exit(){gate.state.fetch_sub(1);}} exit{gate};
    return gate.hook(a,b,c);
}
GateFn bind(uint32_t slot,GateFn fallback,GateFn hook){
    if(slot>=4||!fallback||!hook){SetLastError(ERROR_INVALID_PARAMETER);return nullptr;}
    AcquireSRWLockExclusive(&lock);GateFn result=nullptr;
    if(stopped)SetLastError(ERROR_SHUTDOWN_IN_PROGRESS);
    else {auto& gate=gates[slot];if(!gate.fallback){gate.fallback=fallback;gate.hook=hook;gate.state.store(0);}
        if(gate.fallback!=fallback||gate.hook!=hook)SetLastError(ERROR_ALREADY_EXISTS);
        else {static GateFn functions[]={dispatch<0>,dispatch<1>,dispatch<2>,dispatch<3>};result=functions[slot];}}
    ReleaseSRWLockExclusive(&lock);return result;
}
Word* clone_vtable(const Word* words,size_t count){
    if(!words||count<2||count>SIZE_MAX/sizeof(Word)){SetLastError(ERROR_INVALID_PARAMETER);return nullptr;}
    AcquireSRWLockExclusive(&lock);Word* result=nullptr;
    if(stopped)SetLastError(ERROR_SHUTDOWN_IN_PROGRESS);
    else {auto memory=static_cast<Word*>(VirtualAlloc(nullptr,count*sizeof(Word),MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE));
        if(memory){memcpy(memory,words,count*sizeof(Word));DWORD old=0;if(VirtualProtect(memory,count*sizeof(Word),PAGE_READONLY,&old))result=memory+1;
            else{auto error=GetLastError();VirtualFree(memory,0,MEM_RELEASE);SetLastError(error);}}}
    ReleaseSRWLockExclusive(&lock);return result;
}
void release(Node* node){
    if(!node)return;AcquireSRWLockExclusive(&lock);
    if(--node->references){ReleaseSRWLockExclusive(&lock);return;}
    if(node->previous)node->previous->next=node->next;else head=node->next;
    if(node->next)node->next->previous=node->previous;
    auto destroy=node->destroy;auto context=node->context;if(destroy)extra.fetch_add(1);
    ReleaseSRWLockExclusive(&lock);
    if(destroy){ExtraGuard guard;try{destroy(context);}catch(...){delete node;throw;}}
    delete node;
}
void empty_invoke(Closure*,Word,uint32_t,Word,Word){}
void* empty_manager(Word* control,int operation,Word*,Word,void* destination,size_t){
    if(uint32_t(operation)<=1){if(control){control[0]=reinterpret_cast<Word>(empty_manager);control[1]=reinterpret_cast<Word>(empty_invoke);}return reinterpret_cast<void*>(empty_manager);}
    if(uint32_t(operation)>=4&&destination){*static_cast<Word*>(destination)=1;return destination;}return nullptr;
}
void empty(Closure* closure){*closure={0,0,empty_manager,empty_invoke,0};}
void invoke(Closure* closure,Word,uint32_t status,Word a,Word b){
    auto node=reinterpret_cast<Node*>(closure->state);if(!node)return;
    AcquireSRWLockExclusive(&lock);
    if(stopped||!node->callback){ReleaseSRWLockExclusive(&lock);return;}
    ++node->references;++node->active;extra.fetch_add(1);auto callback=node->callback;auto context=node->context;ReleaseSRWLockExclusive(&lock);
    ExtraGuard count;std::exception_ptr failure;
    try{callback(context,status,a,b);}catch(...){failure=std::current_exception();}
    AcquireSRWLockExclusive(&lock);void* ctx=nullptr;Destroy destroy=nullptr;
    if(--node->active==0&&stopped){ctx=node->context;destroy=node->destroy;node->context=nullptr;node->callback=nullptr;node->destroy=nullptr;}
    ReleaseSRWLockExclusive(&lock);
    try{if(destroy)destroy(ctx);}catch(...){if(!failure)failure=std::current_exception();}
    try{release(node);}catch(...){if(!failure)failure=std::current_exception();}
    if(failure)std::rethrow_exception(failure);
}
void* manage(Word* control,int operation,Word* source,Word,void* destination,size_t size){
    if(operation==0||operation==1){
        if(!control||!source||!destination||size<16){if(destination&&size>=16)memset(destination,0,16);if(control){control[0]=reinterpret_cast<Word>(empty_manager);control[1]=reinterpret_cast<Word>(empty_invoke);}return reinterpret_cast<void*>(empty_manager);}
        auto node=reinterpret_cast<Node*>(source[0]);if(operation==1&&node){AcquireSRWLockExclusive(&lock);++node->references;ReleaseSRWLockExclusive(&lock);}
        memcpy(destination,source,16);if(operation==0){source[0]=0;source[1]=0;}
        control[0]=reinterpret_cast<Word>(manage);control[1]=reinterpret_cast<Word>(invoke);return reinterpret_cast<void*>(manage);
    }
    if(operation==2||operation==3){if(control){auto closure=reinterpret_cast<Closure*>(control-2);auto node=reinterpret_cast<Node*>(closure->state);empty(closure);release(node);}return nullptr;}
    if(operation==4&&destination){*static_cast<Word*>(destination)=0;return destination;}return nullptr;
}
Word make_closure(void* context,Callback callback,Destroy destroy,Closure* result){
    if(!callback||!result){SetLastError(ERROR_INVALID_PARAMETER);return 0;}
    auto node=new(std::nothrow)Node;if(!node){SetLastError(ERROR_NOT_ENOUGH_MEMORY);return 0;}
    AcquireSRWLockExclusive(&lock);
    if(stopped){ReleaseSRWLockExclusive(&lock);delete node;SetLastError(ERROR_SHUTDOWN_IN_PROGRESS);return 0;}
    node->context=context;node->callback=callback;node->destroy=destroy;node->next=head;if(head)head->previous=node;head=node;ReleaseSRWLockExclusive(&lock);
    *result={reinterpret_cast<Word>(node),0,manage,invoke,0};return 1;
}
void quiesce(){
    AcquireSRWLockExclusive(&lock);Node* pending=nullptr;
    if(!stopped){stopped=true;for(auto& gate:gates)gate.state.fetch_or(closed);
        for(auto node=head;node;node=node->next)if(!node->active&&node->callback){
            ++node->references;node->pendingContext=node->context;node->pendingCallback=node->callback;node->pendingDestroy=node->destroy;
            node->context=nullptr;node->callback=nullptr;node->destroy=nullptr;node->pendingNext=pending;pending=node;extra.fetch_add(1);
        }}ReleaseSRWLockExclusive(&lock);
    std::exception_ptr failure;
    while(pending){auto node=pending;pending=node->pendingNext;ExtraGuard count;
        try{if(node->pendingDestroy)node->pendingDestroy(node->pendingContext);}catch(...){if(!failure)failure=std::current_exception();}
        node->pendingContext=nullptr;node->pendingCallback=nullptr;node->pendingDestroy=nullptr;
        try{release(node);}catch(...){if(!failure)failure=std::current_exception();}
    }
    if(failure)std::rethrow_exception(failure);
}
uint64_t active(){AcquireSRWLockExclusive(&lock);uint64_t total=extra.load();for(auto& gate:gates)total+=gate.state.load()&~closed;ReleaseSRWLockExclusive(&lock);return total;}
const Api api{sizeof(Api),0,bind,clone_vtable,quiesce,active,empty,make_closure};
}
extern "C" __declspec(dllexport) const Api* nnp_resident_dispatch_win(){return &api;}

#include <napi.h>
#include "native.hpp"
#include <memory>
namespace {
using namespace recovery;
enum class Op {Processes,Integrity,Holders,Load,ManualLoad,Unload,ManualUnload};
Napi::Object error_object(Napi::Env env,const ReadError& e){auto obj=Napi::Object::New(env);obj.Set("status","error");obj.Set("operation",e.operation);obj.Set("code",e.code);return obj;}
class Work final:public Napi::AsyncWorker {
public:
    Napi::Promise::Deferred deferred;Op op;DWORD pid{};std::wstring path;ManualHandle handle;
    std::vector<DWORD> pids;std::vector<FileHolder> holders;recovery::Integrity integrity;uint32_t number{};
    Work(Napi::Env env,Op which):Napi::AsyncWorker(env),deferred(Napi::Promise::Deferred::New(env)),op(which){}
    void Execute()override{
        try{switch(op){
            case Op::Processes:pids=main_processes();break;
            case Op::Integrity:integrity=runtime_integrity();break;
            case Op::Holders:holders=file_holders(path);break;
            case Op::Load:number=uint32_t(normal_load(pid,path));break;
            case Op::Unload:normal_unload(pid,path);number=0;break;
            case Op::ManualLoad:handle=manual_load(pid,path);break;
            case Op::ManualUnload:manual_unload(pid,handle);break;
        }}catch(const std::exception& e){
            if(op==Op::Load||op==Op::ManualLoad||op==Op::Unload||op==Op::ManualUnload){
                auto detail=dynamic_cast<const LoadError*>(&e);bool timeout=detail&&detail->timeout;
                bool load=op==Op::Load||op==Op::ManualLoad;
                SetError(load?(timeout?"component loading timed out [COMPONENT_LOAD_TIMEOUT]":"component loading failed [COMPONENT_LOAD_FAILED]"):(timeout?"component unloading timed out [COMPONENT_UNLOAD_TIMEOUT]":"component unloading failed [COMPONENT_UNLOAD_FAILED]"));
            }else SetError(e.what());
        }
    }
    void OnError(const Napi::Error& error)override{deferred.Reject(error.Value());}
    void OnOK()override{
        auto env=Env();Napi::HandleScope scope(env);Napi::Value value=env.Undefined();
        switch(op){
        case Op::Processes:{auto a=Napi::Array::New(env,pids.size());for(size_t i=0;i<pids.size();i++)a.Set(uint32_t(i),pids[i]);value=a;break;}
        case Op::Holders:{auto a=Napi::Array::New(env,holders.size());for(size_t i=0;i<holders.size();i++){auto item=Napi::Object::New(env);item.Set("pid",holders[i].pid);item.Set("name",utf8(holders[i].name));a.Set(uint32_t(i),item);}value=a;break;}
        case Op::Integrity:{auto obj=Napi::Object::New(env);obj.Set("pid",integrity.pid);obj.Set("executablePath",integrity.path.empty()?env.Null():Napi::Value(Napi::String::New(env,utf8(integrity.path))));
            if(!integrity.processError.operation.empty())obj.Set("process",error_object(env,integrity.processError));
            else {auto p=Napi::Object::New(env);p.Set("status","ok");p.Set("rid",integrity.processRid);obj.Set("process",p);}
            if(!integrity.fileError.operation.empty())obj.Set("file",error_object(env,integrity.fileError));
            else {auto f=Napi::Object::New(env);f.Set("status","ok");if(!integrity.fileLabel)f.Set("label",env.Null());else{auto l=Napi::Object::New(env);l.Set("rid",integrity.fileRid);l.Set("inherited",integrity.inherited);l.Set("policy",integrity.filePolicy);f.Set("label",l);}obj.Set("file",f);}value=obj;break;}
        case Op::Load:case Op::Unload:value=Napi::Number::New(env,number);break;
        case Op::ManualLoad:{auto obj=Napi::Object::New(env);obj.Set("base",Napi::BigInt::New(env,handle.base));obj.Set("entry",Napi::BigInt::New(env,handle.entry));obj.Set("exceptionTable",Napi::BigInt::New(env,handle.exceptionTable));obj.Set("prepare",Napi::BigInt::New(env,handle.prepare));obj.Set("size",handle.size);value=obj;break;}
        case Op::ManualUnload:break;
        }deferred.Resolve(value);
    }
};
std::wstring string_path(Napi::Value value){if(!value.IsString())throw Napi::Error::New(value.Env(),"path must be a string");auto s=value.As<Napi::String>().Utf16Value();return std::wstring(reinterpret_cast<const wchar_t*>(s.data()),s.size());}
uint64_t bigint(Napi::Object obj,const char* key){auto value=obj.Get(key);bool lossless=false;uint64_t result=0;if(value.IsBigInt())result=value.As<Napi::BigInt>().Uint64Value(&lossless);if(!lossless)throw Napi::Error::New(obj.Env(),std::string("handle.")+key+" must be an unsigned 64-bit BigInt");return result;}
template<Op op>Napi::Value schedule(const Napi::CallbackInfo& info){
    auto env=info.Env();try{
        auto work=std::make_unique<Work>(env,op);
        if constexpr(op==Op::Holders){if(info.Length()<1||!info[0].IsString()||info[0].As<Napi::String>().Utf16Value().empty())throw Napi::TypeError::New(env,"getFileHoldersAsync(path) requires a non-empty path");work->path=string_path(info[0]);}
        if constexpr(op==Op::Load||op==Op::ManualLoad||op==Op::Unload||op==Op::ManualUnload){
            const char* method=op==Op::Load?"loadModuleAsync":op==Op::ManualLoad?"loadModuleManualAsync":op==Op::Unload?"unloadModuleAsync":"unloadModuleManualAsync";
            if(info.Length()<2)throw Napi::Error::New(env,std::string(method)+(op==Op::ManualUnload?"(pid, handle) requires 2 arguments":"(pid, dylibPath) requires 2 arguments"));
            if(!info[0].IsNumber())throw Napi::Error::New(env,"pid must be a uint32");work->pid=info[0].As<Napi::Number>().Uint32Value();
            if constexpr(op==Op::ManualUnload){
                auto obj=info[1].ToObject();work->handle.base=bigint(obj,"base");work->handle.entry=bigint(obj,"entry");work->handle.exceptionTable=bigint(obj,"exceptionTable");work->handle.prepare=bigint(obj,"prepare");
                if(!work->handle.base)throw Napi::Error::New(env,"handle.base must be nonzero");auto size=obj.Get("size");if(!size.IsNumber())throw Napi::Error::New(env,"handle.size must be a uint32");work->handle.size=size.As<Napi::Number>().Uint32Value();
            }else work->path=string_path(info[1]);
        }
        auto promise=work->deferred.Promise();work->Queue();work.release();return promise;
    }catch(const Napi::Error& error){auto d=Napi::Promise::Deferred::New(env);if(env.IsExceptionPending())d.Reject(env.GetAndClearPendingException().Value());else d.Reject(error.Value());return d.Promise();}
    catch(const std::exception& error){auto d=Napi::Promise::Deferred::New(env);d.Reject(Napi::Error::New(env,error.what()).Value());return d.Promise();}
}
Napi::Object init(Napi::Env env,Napi::Object exports){
    exports.DefineProperties({
        Napi::PropertyDescriptor::Function(env,exports,"getAllMainProcessAsync",schedule<Op::Processes>,napi_default),
        Napi::PropertyDescriptor::Function(env,exports,"getRuntimeIntegrityAsync",schedule<Op::Integrity>,napi_default),
        Napi::PropertyDescriptor::Function(env,exports,"getFileHoldersAsync",schedule<Op::Holders>,napi_default),
        Napi::PropertyDescriptor::Function(env,exports,"loadModuleAsync",schedule<Op::Load>,napi_default),
        Napi::PropertyDescriptor::Function(env,exports,"loadModuleManualAsync",schedule<Op::ManualLoad>,napi_default),
        Napi::PropertyDescriptor::Function(env,exports,"unloadModuleAsync",schedule<Op::Unload>,napi_default),
        Napi::PropertyDescriptor::Function(env,exports,"unloadModuleManualAsync",schedule<Op::ManualUnload>,napi_default)});return exports;
}
}
NODE_API_MODULE(snowluma_recovered,init)

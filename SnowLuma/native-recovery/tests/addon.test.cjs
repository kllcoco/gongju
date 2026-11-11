const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {randomUUID}=require('node:crypto');
const root=path.resolve(__dirname,'..'),bin=path.join(root,'build/Release');
const original=require(path.join(root,'reference/original.node'));
const fresh=require(path.join(bin,'snowluma-win32-x64.node'));
const scratch=path.join(root,'work','fixtures 中文');fs.mkdirSync(scratch,{recursive:true});
const fixture=path.join(scratch,'fixture module.dll'),busy=path.join(scratch,'busy module.dll');
fs.copyFileSync(path.join(bin,'fixture_module.dll'),fixture);fs.copyFileSync(path.join(bin,'fixture_busy.dll'),busy);
async function host(executable=path.join(bin,'fixture_host.exe'),preload){
 const prefix='Local\\SnowLumaRecovery.'+randomUUID()+'.';
 const child=spawn(executable,[prefix,...(preload?[preload]:[])],{cwd:scratch,stdio:['pipe','pipe','pipe'],windowsHide:true});
 const lines=[],waiters=[];let finished=false,exitCode;
 let spawnError;
 const ended=new Promise(resolve=>{child.once('error',error=>{spawnError=error;finished=true;resolve(null);while(waiters.length)waiters.shift().reject(error);});child.once('exit',code=>{finished=true;exitCode=code;resolve(code);while(waiters.length)waiters.shift().reject(Error('fixture exited: '+code));});});
 const reader=createInterface({input:child.stdout});reader.on('line',line=>{if(waiters.length)waiters.shift().resolve(line);else lines.push(line);});
 let stderr='';child.stderr.on('data',b=>stderr+=b);
 const next=()=>spawnError?Promise.reject(spawnError):lines.length?Promise.resolve(lines.shift()):finished?Promise.reject(Error('fixture exited: '+exitCode)):new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(Error('fixture response timeout '+stderr)),10000);
  waiters.push({resolve:x=>{clearTimeout(timer);resolve(x);},reject:e=>{clearTimeout(timer);reject(e);}});
 });
 let ready;try{ready=await next();assert.match(ready,/^READY \d+$/);}catch(error){reader.close();if(!finished)child.kill();throw error;}
 return {pid:Number(ready.split(' ')[1]),async command(s){child.stdin.write(s+'\n');return next();},async close(){if(!finished)child.stdin.end('quit\n');const timer=setTimeout(()=>child.kill(),10000);await ended;clearTimeout(timer);reader.close();assert.equal(exitCode,0);}};
}
async function failure(addon,name,args){let value;try{value=addon[name](...args);}catch(e){return {synchronous:true,name:e.name,message:e.message};}assert.ok(value instanceof Promise);try{await value;return null;}catch(e){return {synchronous:false,name:e.name,message:e.message};}}
test('seven methods and property attributes match the original',()=>{
 assert.deepEqual(Object.getOwnPropertyNames(fresh),Object.getOwnPropertyNames(original));
 for(const name of Object.getOwnPropertyNames(original)){const a=Object.getOwnPropertyDescriptor(original,name),b=Object.getOwnPropertyDescriptor(fresh,name);assert.equal(typeof b.value,'function');for(const key of ['enumerable','configurable','writable'])assert.equal(b[key],a[key]);}
});
test('invalid arguments preserve Promise rejection and error messages',async()=>{
 for(const name of ['getFileHoldersAsync','loadModuleAsync','loadModuleManualAsync','unloadModuleAsync','unloadModuleManualAsync']){
  for(const args of [[],[0],[0,'not-a-real-file'],[process.pid,null]])assert.deepEqual(await failure(fresh,name,args),await failure(original,name,args),name+' '+String(args));
 }
});
test('runtime integrity matches original in the same process',async()=>{assert.deepEqual(await fresh.getRuntimeIntegrityAsync(),await original.getRuntimeIntegrityAsync());});
test('Restart Manager handles existing, missing and directory paths',async()=>{
 const file=path.join(scratch,'held file.txt');fs.writeFileSync(file,'isolated fixture');const fd=fs.openSync(file,'r+');
 try{const normalize=a=>a.sort((x,y)=>x.pid-y.pid||x.name.localeCompare(y.name));assert.deepEqual(normalize(await fresh.getFileHoldersAsync(file)),normalize(await original.getFileHoldersAsync(file)));}
 finally{fs.closeSync(fd);}
 assert.deepEqual(await fresh.getFileHoldersAsync(path.join(scratch,'absent.txt')),[]);
 assert.deepEqual(await failure(fresh,'getFileHoldersAsync',[scratch]),await failure(original,'getFileHoldersAsync',[scratch]));
});
for(const [label,addon] of [['original',original],['rebuilt',fresh]]){
 test(label+': ordinary load/unload runs attach, prepare and detach',async()=>{
  const h=await host();try{const value=await addon.loadModuleAsync(h.pid,fixture);assert.equal(typeof value,'number');assert.equal(await h.command('events'),'1');assert.equal(await addon.unloadModuleAsync(h.pid,fixture),0);assert.equal(await h.command('events'),'7');}finally{await h.close();}
 });
 test(label+': manual mapping has five fields and unload releases image',async()=>{
  const h=await host();try{const value=await addon.loadModuleManualAsync(h.pid,fixture);assert.deepEqual(Object.keys(value).sort(),['base','entry','exceptionTable','prepare','size'].sort());for(const k of ['base','entry','exceptionTable','prepare'])assert.equal(typeof value[k],'bigint');assert.ok(value.base>0n&&value.prepare>value.base&&value.size>0);assert.equal(await h.command('events'),'1');assert.equal(await h.command('probe '+value.base.toString(16)),'mapped');assert.equal(await addon.unloadModuleManualAsync(h.pid,value),undefined);assert.equal(await h.command('events'),'7');assert.equal(await h.command('probe '+value.base.toString(16)),'free');}finally{await h.close();}
 });
 test(label+': busy prepare callback retains the mapped image',async()=>{
  const h=await host();try{const value=await addon.loadModuleManualAsync(h.pid,busy);await assert.rejects(addon.unloadModuleManualAsync(h.pid,value),/COMPONENT_UNLOAD_FAILED/);assert.equal(await h.command('events'),'3');assert.equal(await h.command('probe '+value.base.toString(16)),'mapped');}finally{await h.close();}
 });
}
test('QQ discovery requires wrapper.node in an isolated fixture process',async()=>{
 const exe=path.join(scratch,'QQ.exe'),wrapper=path.join(scratch,'wrapper.node');fs.copyFileSync(path.join(bin,'fixture_host.exe'),exe);fs.copyFileSync(fixture,wrapper);
 const bare=await host(exe);try{assert.ok(!(await original.getAllMainProcessAsync()).includes(bare.pid));assert.ok(!(await fresh.getAllMainProcessAsync()).includes(bare.pid));}finally{await bare.close();}
 const wrapped=await host(exe,wrapper);try{assert.ok((await original.getAllMainProcessAsync()).includes(wrapped.pid));assert.ok((await fresh.getAllMainProcessAsync()).includes(wrapped.pid));}finally{await wrapped.close();}
});

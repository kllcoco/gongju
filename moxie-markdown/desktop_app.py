import json,os,sys,time,secrets,threading,queue
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
import native_base
from core import BASE,DATA,ASSETS,atomic,sha,backup,decode,bounded
from rendering import Renderer,export_pdf,Tree
import markdown
from outline import source_headings

from syntax_tools import SyntaxTools

class App(SyntaxTools,native_base.App):
    def __init__(self,root):
        self.session=str(os.getpid())+'-'+secrets.token_hex(4);self.draft=DATA/'drafts'/(self.session+'.json');self.lastdraft=None;self.renderer=None;self.external_digest=None
        super().__init__(root)
        root.title('墨写 · Markdown');root.minsize(1040,620)
        try:root.iconbitmap(str(ASSETS/'moxie.ico'))
        except tk.TclError:pass
        self.filemenu.insert_command(6,label='导出 PDF…',command=self.pdf)
        self.filemenu.insert_command(7,label='恢复草稿…',command=self.restore)
        self.filemenu.insert_command(8,label='历史版本…',command=self.history)
        self.filemenu.insert_command(9,label='打开 MCP 配置说明',command=self.show_mcp_guide)
        self.helpmenu.add_command(label='MCP 接入说明',command=self.show_mcp_guide)
        self.root.bind('<Return>',self.enter)
        for seq,d in [('<Control-plus>',1),('<Control-equal>',1),('<Control-KP_Add>',1),('<Control-minus>',-1),('<Control-KP_Subtract>',-1)]:self.root.bind(seq,lambda e,d=d:self.zoom(d))
        self.bind_editor_keys()
        self.tree.bind("<ButtonRelease-1>",self.outline_click)
        self.tree.bind("<Return>",lambda e:(self.jump(),"break")[1])
        self.setup_syntax();self.apply_text_theme()
        self.start_bridge();self.root.after(2000,self.tick)
        self.root.after(500,self.offer_restore)
    def bind_editor_keys(self):
        # Tk's Text class ships Emacs-style keys (Ctrl+I inserts a tab over the selection, Ctrl+O inserts a
        # newline, Ctrl+H deletes a character, ...). Class bindings run before window bindings, so the
        # shortcuts are bound on the editor itself and stop with 'break'.
        actions={'n':self.new,'o':self.open,'s':self.save,'f':self.find,'h':self.find,
                 'b':lambda:self.syntax_commands['粗体'](),'i':lambda:self.syntax_commands['斜体']()}
        for key,cmd in actions.items():
            for seq in ('<Control-%s>'%key,'<Control-%s>'%key.upper()):
                self.source.bind(seq,lambda e,c=cmd:(c(),'break')[1])
        self.source.bind('<Control-S>',lambda e:(self.save(True),'break')[1])
        for key in 'dktpae':self.source.bind('<Control-%s>'%key,lambda e:'break')
        self.source.bind('<Control-a>',lambda e:(self.select_all(),'break')[1])
        self.source.bind('<Control-e>',lambda e:'break')
    def show_mcp_guide(self):
        from viewer import DocViewer
        return DocViewer(self,BASE/'MCP接入说明.md','MCP 接入说明')
    def open_link(self,url,base=None):
        from urllib.parse import urlparse,unquote
        if url.startswith(('http://','https://','mailto:')):
            import webbrowser;webbrowser.open(url);return 'external'
        if url.startswith('#'):
            target=unquote(url[1:]).strip().lower()
            for h in self.renderer.headings if self.renderer else []:
                if h['text'].strip().lower().replace(' ','-')==target or h['text'].strip().lower()==target:
                    self.preview.yview(h['mark']);return 'anchor'
            return None
        base=Path(base) if base else (self.path.parent if self.path else BASE)
        target=Path(unquote(urlparse(url).path or url))
        target=target if target.is_absolute() else (base/target)
        if target.suffix.lower() in ('.md','.markdown','.mdown','.mkd') and target.is_file():
            if self.confirm():self.load(target.resolve());return 'opened'
            return None
        if target.exists():
            if messagebox.askyesno('打开文件',f'墨写只能打开 Markdown 文件。\n是否用系统默认程序打开：\n{target.name}？',parent=self.root):
                try:os.startfile(str(target))
                except (AttributeError,OSError) as e:messagebox.showerror('打开失败',str(e),parent=self.root)
            return 'asked'
        messagebox.showinfo('链接','找不到链接指向的文件：\n'+str(target),parent=self.root);return None
    def show_about(self,query=''):
        from usage_guide import UsageGuide
        guide=getattr(self,'usage_guide',None)
        if guide is None or not guide.window.winfo_exists():
            guide=UsageGuide(self);self.usage_guide=guide
        else:guide.window.deiconify();guide.window.lift()
        if query:guide.query.set(query);guide.filter_topics()
        return guide
    def render(self):
        self._rebuilding_outline=True
        selected=self.tree.selection()
        self.job=None;pos=self.preview.yview();top=self.preview.index('@0,0');self.preview.config(state='normal');self.preview.delete('1.0','end')
        self.renderer=Renderer(self.preview,self.path.parent if self.path else BASE,self.p,self.open_link,self.fonts['ui']);self.renderer.render(self.html());self.preview.config(state='disabled');self.preview.yview_moveto(pos[0]) if pos[0]<=0 else self.preview.yview(top);self.tree.delete(*self.tree.get_children())
        import re
        self.outline_targets={};cursor=0
        for line,level,title in source_headings(self.content()):
            for i in range(cursor,len(self.renderer.headings)):
                heading=self.renderer.headings[i]
                if heading['level']==level and heading['text']==title:
                    key=str(line);self.tree.insert('','end',iid=key,text='   '*(level-1)+title,tags=('lvl'+str(level),))
                    self.outline_targets[key]=heading['mark'];cursor=i+1;break
        if selected and self.tree.exists(selected[0]):self.tree.selection_set(selected[0])
        self.root.after_idle(lambda:setattr(self,'_rebuilding_outline',False))
        for name in ('md_heading','md_code','md_codeblock','md_quote','md_list'):self.source.tag_remove(name,'1.0','end')
        fence=None
        for n,line in enumerate(self.content().splitlines(),1):
            marker=re.match(r'^ {0,3}(`{3,}|~{3,})',line)
            if fence or marker:
                self.source.tag_add('md_codeblock',f'{n}.0',f'{n}.end+1c')
                if marker:
                    self.source.tag_add('md_code',f'{n}.0',f'{n}.end')
                    if fence is None:fence=marker[1]
                    elif marker[1][0]==fence[0] and len(marker[1])>=len(fence):fence=None
                continue
            if re.match(r'^ {0,3}#{1,6}\s',line):self.source.tag_add('md_heading',f'{n}.0',f'{n}.end')
            elif line.lstrip().startswith('>'):self.source.tag_add('md_quote',f'{n}.0',f'{n}.end')
            else:
                m=re.match(r'^(\s*)([-*+]|\d+[.)])( \[[ xX]\])? ',line)
                if m:self.source.tag_add('md_list',f'{n}.{len(m[1])}',f'{n}.{m.end()}')
        self.source.tag_raise('sel')
        self.update_status()
    def jump(self,event=None):
        if getattr(self,'_rebuilding_outline',False):return
        selected=self.tree.selection()
        if not selected:return
        key=selected[0];index=key+'.0';mark=self.outline_targets.get(key)
        self.source.mark_set('insert',index);self.source.yview(index)
        self.source.tag_configure('outline_target',background='#d8eee6')
        self.source.tag_remove('outline_target','1.0','end');self.source.tag_add('outline_target',index,index+' lineend')
        if mark and mark in self.preview.mark_names():
            self.preview.yview(mark);self.preview.tag_configure('outline_target',background='#d8eee6')
            self.preview.tag_remove('outline_target','1.0','end');self.preview.tag_add('outline_target',mark,mark+' lineend')
        self.update_status()
    def outline_click(self,event):
        item=self.tree.identify_row(event.y)
        if item:self.tree.selection_set(item);self.jump()
    def enter(self,e):
        if e.widget!=self.source:return
        import re
        line=self.source.get('insert linestart','insert')
        task=re.match(r'^(\s*)([-*+] )\[[ xX]\] (.*)$',line)
        if task:
            if not task[3].strip():self.source.delete('insert linestart','insert');self.source.insert('insert','\n')
            else:self.source.insert('insert','\n'+task[1]+task[2]+'[ ] ')
            return 'break'
        match=re.match(r'^(\s*)([-*+] |\d+\. |>(?: )?)?',line)
        prefix=match[0] if match else ''
        if prefix.strip() and line.strip()==prefix.strip():self.source.delete('insert linestart','insert');self.source.insert('insert','\n');return 'break'
        numbered=re.match(r'^(\s*)(\d+)\. $',prefix)
        if numbered:prefix=numbered[1]+str(int(numbered[2])+1)+'. '
        self.source.insert('insert','\n'+prefix);return 'break'
    def insert(self,before,after='',line=False):
        if line:
            self.source.focus_set();self.source.edit_separator();self.source.configure(autoseparators=False)
            try:start,end=self.source.index('sel.first linestart'),self.source.index('sel.last')
            except tk.TclError:start,end=self.source.index('insert linestart'),self.source.index('insert lineend')
            text=self.source.get(start,end);self.source.delete(start,end);self.source.insert(start,'\n'.join(before+s for s in text.split('\n')));self.source.edit_separator();self.source.configure(autoseparators=True)
        else:super().insert(before,after,line)
    def checkpoint(self):
        if self.dirty():
            payload={'path':str(self.path) if self.path else None,'text':self.content(),'baseline':self.saved,'digest':self.digest,'encoding':self.encoding,'updated':time.time()}
            raw=json.dumps(payload,ensure_ascii=False).encode()
            if raw!=self.lastdraft:atomic(self.draft,raw);self.lastdraft=raw
        elif self.draft.exists():self.draft.unlink();self.lastdraft=None
    def tick(self):
        try:
            self.checkpoint()
            if self.path and self.path.exists():
                raw=self.path.read_bytes();digest=sha(raw)
                if digest!=self.digest and digest!=self.external_digest:
                    if not self.dirty():self.load(self.path)
                    else:self.status.config(text='磁盘文件已被修改。当前编辑内容已保留；保存时将再次确认。');self.external_digest=digest
        except OSError:pass
        self.root.after(2000,self.tick)
    def confirm(self):
        result=super().confirm()
        if result and self.draft.exists():self.draft.unlink();self.lastdraft=None
        return result
    def save(self,saveas=False):
        # Preserve prior on-disk versions before all ordinary saves.
        if self.path and self.path.exists() and not saveas:
            try:backup(self.path,self.path.read_bytes())
            except OSError as e:messagebox.showerror('备份失败',str(e));return False
        result=super().save(saveas)
        if result:self.checkpoint()
        return result
    def restore_candidates(self):
        active=set()
        for p in (DATA/'sessions').glob('*.json'):
            try:
                import urllib.request
                info=json.loads(p.read_text('utf-8'));request=urllib.request.Request(info['url'],data=b'{"action":"ping"}',headers={'X-Moxie-Token':info['token'],'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=.2):active.add(p.stem)
            except Exception:pass
        return [p for p in (DATA/'drafts').glob('*.json') if p!=self.draft and p.stem not in active]
    def offer_restore(self):
        candidates=self.restore_candidates()
        if candidates and messagebox.askyesno('发现恢复草稿','上次有未保存的内容。是否查看并恢复？'):self.restore()
    def restore(self):
        candidates=self.restore_candidates()
        if not candidates:messagebox.showinfo('恢复草稿','没有其他窗口留下的恢复草稿。');return
        window=tk.Toplevel(self.root);window.title('恢复草稿');window.geometry('620x330');window.configure(bg=self.p['bg']);box=tk.Listbox(window,font=(self.fonts['ui'],11));box.pack(fill='both',expand=True,padx=15,pady=15)
        for p in candidates:
            data=json.loads(p.read_text('utf-8'));box.insert('end',time.strftime('%m-%d %H:%M',time.localtime(data['updated']))+'  '+(data['path'] or '未命名文档'))
        def apply():
            if not box.curselection() or not self.confirm():return
            p=candidates[box.curselection()[0]];d=json.loads(p.read_text('utf-8'));self.path=Path(d['path']) if d['path'] else None;self.encoding=d.get('encoding','utf-8');self.digest=d.get('digest');self.settext(d['text']);self.saved=d.get('baseline','');self.update_status();self.checkpoint();window.destroy()
        ttk.Button(window,text='恢复选中草稿',command=apply).pack(pady=10)
    def history(self):
        if not self.path:messagebox.showinfo('历史版本','保存文件后才会产生历史版本。');return
        folder=DATA/'history'/sha(str(self.path.resolve()).encode())[:24];files=sorted(folder.glob('*.bak'),reverse=True)
        if not files:messagebox.showinfo('历史版本','还没有历史版本。');return
        win=tk.Toplevel(self.root);win.title('历史版本 · 恢复后需手动保存');win.geometry('530x350');win.configure(bg=self.p['bg']);box=tk.Listbox(win,font=(self.fonts['ui'],11));box.pack(fill='both',expand=True,padx=14,pady=14)
        for p in files:box.insert('end',time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(int(p.stem)/1e9))+f'   {p.stat().st_size} 字节')
        def restore():
            if not box.curselection() or not self.confirm():return
            text=decode(files[box.curselection()[0]].read_bytes());self.source.edit_separator();self.source.delete('1.0','end');self.source.insert('1.0',text);self.source.edit_separator();self.render();self.checkpoint();win.destroy()
        ttk.Button(win,text='恢复到编辑区',command=restore).pack(pady=10)
    def pdf(self):
        name=filedialog.asksaveasfilename(defaultextension='.pdf',initialfile=(self.path.stem if self.path else '未命名')+'.pdf',filetypes=[('PDF 文档','*.pdf')])
        if name:
            try:export_pdf(self.html(),name,self.path.parent if self.path else BASE);messagebox.showinfo('导出完成','PDF 文件已保存。')
            except Exception as e:messagebox.showerror('导出失败',str(e))
    def start_bridge(self):
        self.jobs=queue.Queue();self.token=secrets.token_urlsafe(32);app=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.headers.get('X-Moxie-Token')!=app.token:self.send_error(403);return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if length<0 or length>24*1024*1024:raise ValueError('Request too large')
                    data=json.loads(self.rfile.read(length));event=threading.Event();result={}
                    if data.get('action')=='ping':result['value']={'session':app.session}
                    else:
                        app.jobs.put((data,event,result))
                        if not event.wait(15):result['cancelled']=True;raise ValueError('Editor busy; request cancelled')
                    raw=json.dumps(result,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except Exception as e:
                    try:self.send_error(400,str(e))
                    except OSError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True;threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.sessionfile=DATA/'sessions'/(self.session+'.json');atomic(self.sessionfile,json.dumps({'url':f'http://127.0.0.1:{self.server.server_port}/','token':self.token}).encode());self.root.after(80,self.pump)
    def pump(self):
        try:
            while True:
                data,event,result=self.jobs.get_nowait()
                if result.get('cancelled'):event.set();continue
                try:result['value']=self.handle(data)
                except Exception as e:result['error']=str(e)
                finally:event.set()
        except queue.Empty:pass
        self.root.after(80,self.pump)
    def handle(self,data):
        root=Path(data['root']).resolve()
        if self.path:bounded(root,str(self.path.resolve()))
        action=data['action'];version=sha(self.content().encode())
        if action in ('replace','save') and data.get('expected_version')!=version:raise ValueError('Document changed; read it again before editing or saving')
        if action=='replace':
            if len(data['text'].encode())>20*1024*1024:raise ValueError('Document exceeds 20 MB')
            self.source.edit_separator();self.source.configure(autoseparators=False);self.source.delete('1.0','end');self.source.insert('1.0',data['text']);self.source.edit_separator();self.source.configure(autoseparators=True);self.render();self.checkpoint()
        elif action=='save':
            if not self.path:raise ValueError('Untitled document: save it in the editor first')
            if not self.path.exists() or sha(self.path.read_bytes())!=self.digest:raise ValueError('Disk file changed; resolve in the editor')
            if not self.save():raise ValueError('Save did not complete')
        elif action!='read':raise ValueError('Unknown editor action')
        return {'session':self.session,'path':str(self.path) if self.path else None,'text':self.content(),'version':sha(self.content().encode()),'dirty':self.dirty()}
    def close(self):
        if self.confirm():
            if self.draft.exists():self.draft.unlink()
            self.sessionfile.unlink(missing_ok=True);self.server.shutdown();self.server.server_close();self.root.destroy()

def main():
    root=tk.Tk();App(root);root.mainloop()
if __name__=='__main__':main()

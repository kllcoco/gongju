import sys, os, re, json, hashlib, tempfile, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font
from html.parser import HTMLParser
from core import BASE,DATA,ASSETS
sys.path.insert(0,str(BASE/'vendor'))
import markdown

class Preview(HTMLParser):
    def __init__(self,widget):
        super().__init__(); self.w=widget; self.stack=[]; self.links=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('p','div','blockquote','pre','table') or re.fullmatch('h[1-6]',tag): self.w.insert('end','\n')
        if tag=='li': self.w.insert('end','  •  ')
        if tag=='br': self.w.insert('end','\n')
        if tag=='hr': self.w.insert('end','\n'+'─'*45+'\n','muted')
        if tag=='img': self.w.insert('end','[图片：'+attrs.get('alt','')+']','muted')
        style={'strong':'bold','em':'italic','code':'code','pre':'code','blockquote':'quote','th':'bold'}.get(tag,tag)
        if tag=='a':
            style='link'+str(len(self.links)); url=attrs.get('href',''); self.links.append(url)
            self.w.tag_configure(style,foreground='#087c72',underline=True)
            self.w.tag_bind(style,'<Button-1>',lambda e,u=url: webbrowser.open(u) if u.startswith(('http://','https://')) else None)
        if tag not in ('br','hr','img','input'): self.stack.append((tag,style))
    def handle_endtag(self,tag):
        if tag in ('p','li','blockquote','pre','tr') or re.fullmatch('h[1-6]',tag): self.w.insert('end','\n')
        if tag in ('td','th'): self.w.insert('end','    │    ')
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag: self.stack=self.stack[:i]; break
    def handle_data(self,data):
        self.w.insert('end',data,tuple(s for _,s in self.stack))

class App:
    def __init__(self,root):
        self.root=root; self.path=None; self.saved=''; self.digest=None; self.encoding='utf-8'; self.job=None; self.size=12
        self.settings=DATA/'preferences.json'; self.recent=[]
        try: self.recent=json.loads(self.settings.read_text('utf-8')).get('recent',[])
        except Exception: pass
        root.title('墨写 · Markdown'); root.geometry('1180x780'); root.minsize(820,540); root.configure(bg='#f1f4f8')
        style=ttk.Style(); style.theme_use('clam'); style.configure('TFrame',background='#f1f4f8'); style.configure('TButton',padding=(10,7),font=('Microsoft YaHei UI',10)); style.configure('TLabel',background='#f1f4f8',font=('Microsoft YaHei UI',10)); style.configure('Treeview',rowheight=29,font=('Microsoft YaHei UI',10))
        menu=tk.Menu(root); root.config(menu=menu)
        self.filemenu=tk.Menu(menu,tearoff=False); menu.add_cascade(label='文件',menu=self.filemenu)
        for title,cmd,shortcut in [('新建',self.new,'Ctrl+N'),('打开…',self.open,'Ctrl+O'),('保存',self.save,'Ctrl+S'),('另存为…',lambda:self.save(True),'Ctrl+Shift+S'),('导出 HTML…',self.export,''),('退出',self.close,'')]: self.filemenu.add_command(label=title,command=cmd,accelerator=shortcut)
        self.recentmenu=tk.Menu(self.filemenu,tearoff=False); self.filemenu.insert_cascade(2,label='最近打开',menu=self.recentmenu); self.refresh_recent()
        edit=tk.Menu(menu,tearoff=False); menu.add_cascade(label='编辑',menu=edit)
        for label,cmd in [('撤销',lambda:self.event('<<Undo>>')),('重做',lambda:self.event('<<Redo>>')),('剪切',lambda:self.event('<<Cut>>')),('复制',lambda:self.event('<<Copy>>')),('粘贴',lambda:self.event('<<Paste>>')),('全选',self.select_all),('查找 / 替换',self.find)]: edit.add_command(label=label,command=cmd)
        view=tk.Menu(menu,tearoff=False); menu.add_cascade(label='视图',menu=view)
        for label,cmd in [('编辑 + 预览',lambda:self.mode('both')),('专注编辑',lambda:self.mode('edit')),('阅读模式',lambda:self.mode('read')),('放大文字',lambda:self.zoom(1)),('缩小文字',lambda:self.zoom(-1))]: view.add_command(label=label,command=cmd)
        helpm=tk.Menu(menu,tearoff=False); menu.add_cascade(label='帮助',menu=helpm); helpm.add_command(label='Markdown 语法指南',command=lambda:self.load(BASE/'示例.md') if self.confirm() else None); helpm.add_command(label='关于',command=lambda:messagebox.showinfo('墨写','墨写 Markdown 桌面编辑器\n本地运行 · 轻量书写\n支持常用 Markdown、表格、代码块和脚注。'))
        top=ttk.Frame(root,padding=(18,12)); top.pack(fill='x'); ttk.Label(top,text='墨写',font=('Microsoft YaHei UI',21,'bold'),foreground='#187b70').pack(side='left'); self.name=ttk.Label(top,text='未命名.md',padding=(20,0)); self.name.pack(side='left')
        for label,cmd in [('查找',self.find),('保存',self.save),('打开',self.open),('新建',self.new)]: ttk.Button(top,text=label,command=cmd).pack(side='right',padx=3)
        self.outer=ttk.Panedwindow(root,orient='horizontal'); self.outer.pack(fill='both',expand=True,padx=14)
        outline=ttk.Frame(self.outer,width=170); ttk.Label(outline,text='文档大纲',padding=10).pack(anchor='w'); self.tree=ttk.Treeview(outline,show='tree',selectmode='browse'); self.tree.pack(fill='both',expand=True); self.tree.bind('<<TreeviewSelect>>',self.jump); self.outer.add(outline,weight=0)
        self.split=ttk.Panedwindow(self.outer,orient='horizontal'); self.outer.add(self.split,weight=1)
        self.sourceframe,self.previewframe=ttk.Frame(self.split),ttk.Frame(self.split)
        self.source=self.textpane(self.sourceframe,'MARKDOWN · 编辑',True); self.preview=self.textpane(self.previewframe,'阅读预览',False)
        self.split.add(self.sourceframe,weight=1); self.split.add(self.previewframe,weight=1)
        for i in range(1,7): self.preview.tag_configure('h'+str(i),font=('Microsoft YaHei UI',max(13,25-i*2),'bold'),spacing1=12,spacing3=8)
        for name,options in {'bold':{'font':('Microsoft YaHei UI',12,'bold')},'italic':{'font':('Microsoft YaHei UI',12,'italic')},'code':{'font':('Consolas',11),'background':'#eef2f6'},'quote':{'foreground':'#697a87','lmargin1':16,'lmargin2':16},'muted':{'foreground':'#88939f'}}.items(): self.preview.tag_configure(name,**options)
        self.status=ttk.Label(root,padding=(18,10)); self.status.pack(fill='x'); self.source.bind('<<Modified>>',self.changed); self.source.bind('<KeyRelease>',lambda e:self.update_status()); self.source.bind('<ButtonRelease-1>',lambda e:self.update_status()); self.source.bind('<Tab>',self.tab);self.source.bind('<Control-y>',lambda e:(self.event('<<Redo>>'),'break')[1])
        for key,cmd in [('n',self.new),('o',self.open),('s',self.save),('f',self.find),('h',self.find),('b',lambda:self.insert('**','**')),('i',lambda:self.insert('*','*'))]: root.bind('<Control-'+key+'>',lambda e,c=cmd:(c(),'break')[1])
        root.bind('<Control-Shift-S>',lambda e:self.save(True)); root.protocol('WM_DELETE_WINDOW',self.close)
        self.render(); self.source.focus_set()
        if len(sys.argv)>1: self.load(Path(sys.argv[1]))
    def textpane(self,frame,label,editable):
        ttk.Label(frame,text=label,padding=10,foreground='#718093').pack(anchor='w'); inner=ttk.Frame(frame); inner.pack(fill='both',expand=True)
        text=tk.Text(inner,wrap='word',undo=editable,maxundo=200,autoseparators=True,font=('Consolas' if editable else 'Microsoft YaHei UI',12),bg='#ffffff',fg='#263445',insertbackground='#187b70',selectbackground='#bce4dc',relief='flat',padx=22,pady=18,spacing1=2,spacing3=3)
        scroll=ttk.Scrollbar(inner,command=text.yview); scroll.pack(side='right',fill='y'); text.configure(yscrollcommand=scroll.set); text.pack(fill='both',expand=True); return text
    def content(self): return self.source.get('1.0','end-1c')
    def dirty(self): return self.content()!=self.saved
    def event(self,name):
        try:self.source.event_generate(name)
        except tk.TclError:pass
    def select_all(self): self.source.tag_add('sel','1.0','end-1c'); self.source.focus_set()
    def changed(self,event=None):
        if not self.source.edit_modified():return
        self.source.edit_modified(False)
        if self.job:self.root.after_cancel(self.job)
        self.job=self.root.after(220,self.render); self.update_status()
    def update_status(self):
        text=self.content(); name=self.path.name if self.path else '未命名.md'; self.name.config(text=name+('  ● 未保存' if self.dirty() else ''))
        self.root.title(('* ' if self.dirty() else '')+name+' — 墨写 Markdown')
        self.status.config(text=f'{len(text):,} 字符   ·   {text.count(chr(10))+1} 行   ·   光标 {self.source.index("insert")}   ·   {self.encoding.upper()}'+('   ·   '+str(self.path) if self.path else ''))
    def html(self): return markdown.markdown(self.content(),extensions=['extra','sane_lists','nl2br'])
    def render(self):
        self.job=None; pos=self.preview.yview(); self.preview.config(state='normal'); self.preview.delete('1.0','end'); Preview(self.preview).feed(self.html()); self.preview.config(state='disabled'); self.preview.yview_moveto(pos[0]); self.tree.delete(*self.tree.get_children())
        fence=False
        for n,line in enumerate(self.content().splitlines(),1):
            if line.startswith(('```','~~~')):fence=not fence
            match=re.match(r'^(#{1,6})\s+(.+)',line)
            if match and not fence:self.tree.insert('','end',iid=str(n),text='  '*(len(match[1])-1)+match[2])
        self.update_status()
    def jump(self,e):
        if self.tree.selection(): self.source.see(self.tree.selection()[0]+'.0'); self.source.mark_set('insert',self.tree.selection()[0]+'.0')
    def confirm(self):
        if not self.dirty():return True
        answer=messagebox.askyesnocancel('保存修改','当前文档尚未保存，是否保存？',parent=self.root)
        return self.save() if answer else answer is False
    def new(self):
        if self.confirm():self.path=None; self.encoding='utf-8'; self.settext('')
    def settext(self,text):self.source.delete('1.0','end'); self.source.insert('1.0',text); self.saved=text; self.source.edit_reset(); self.source.edit_modified(False); self.render()
    def open(self):
        if not self.confirm():return
        name=filedialog.askopenfilename(filetypes=[('Markdown','*.md *.markdown *.mdown *.mkd'),('文本','*.txt'),('所有文件','*.*')])
        if name:self.load(Path(name))
    def load(self,path):
        try:
            raw=path.read_bytes()
            if len(raw)>20*1024*1024:raise ValueError('文件超过 20 MB。')
            for enc in (['utf-16'] if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else ['utf-8-sig','gb18030']):
                try:text=raw.decode(enc); break
                except UnicodeError:continue
            else:raise ValueError('无法识别文本编码。')
            self.encoding='utf-8' if enc=='utf-8-sig' and not raw.startswith(b'\xef\xbb\xbf') else enc; self.path=path; self.digest=hashlib.sha256(raw).hexdigest(); self.settext(text.replace('\r\n','\n')); self.remember()
        except Exception as e:messagebox.showerror('打开失败',str(e))
    def save(self,saveas=False):
        path=self.path
        if saveas or path is None:
            name=filedialog.asksaveasfilename(defaultextension='.md',initialfile=path.name if path else '未命名.md',filetypes=[('Markdown','*.md'),('所有文件','*.*')])
            if not name:return False
            path=Path(name)
        try:
            if path==self.path and path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=self.digest:
                if not messagebox.askyesno('文件已变化','文件已被其他程序修改，是否覆盖？'):return False
            enc='utf-8' if saveas or self.path is None else self.encoding
            try:raw=self.content().encode(enc)
            except UnicodeError:enc='utf-8';raw=self.content().encode(enc)
            tmp=None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:tmp=f.name;f.write(raw);f.flush();os.fsync(f.fileno())
                os.replace(tmp,path)
            finally:
                if tmp and os.path.exists(tmp):os.unlink(tmp)
            self.path=path; self.encoding=enc; self.digest=hashlib.sha256(raw).hexdigest(); self.saved=self.content(); self.remember(); self.update_status(); return True
        except Exception as e:messagebox.showerror('保存失败',str(e));return False
    def remember(self):
        self.recent=[str(self.path)]+[p for p in self.recent if p!=str(self.path)];self.recent=self.recent[:10]
        try:self.settings.write_text(json.dumps({'recent':self.recent}),encoding='utf-8')
        except OSError:pass
        self.refresh_recent()
    def refresh_recent(self):
        self.recentmenu.delete(0,'end')
        for p in self.recent:self.recentmenu.add_command(label=p,command=lambda p=p:self.load(Path(p)) if self.confirm() else None)
    def insert(self,before,after='',line=False):
        self.source.focus_set(); self.source.edit_separator(); self.source.configure(autoseparators=False)
        try:start,end=self.source.index('sel.first'),self.source.index('sel.last');text=self.source.get(start,end)
        except tk.TclError:start=end=self.source.index('insert');text=''
        if line:start=self.source.index(start+' linestart')
        self.source.delete(start,end);self.source.insert(start,before+text+after);self.source.edit_separator();self.source.configure(autoseparators=True)
    def tab(self,e):self.insert('    ');return 'break'
    def image(self):
        name=filedialog.askopenfilename(filetypes=[('图片','*.png *.jpg *.jpeg *.gif *.webp')])
        if name:
            path=os.path.relpath(name,self.path.parent) if self.path else name
            self.insert('![图片]('+path.replace('\\','/')+')')
    def find(self):
        win=tk.Toplevel(self.root);win.title('查找与替换');win.geometry('460x190');win.transient(self.root)
        q=tk.StringVar();r=tk.StringVar(); ttk.Label(win,text='查找').grid(row=0,column=0,padx=12,pady=12); entry=ttk.Entry(win,textvariable=q,width=40);entry.grid(row=0,column=1,columnspan=3);ttk.Label(win,text='替换为').grid(row=1,column=0,padx=12);ttk.Entry(win,textvariable=r,width=40).grid(row=1,column=1,columnspan=3)
        def search():
            if not q.get():return
            pos=self.source.search(q.get(),'insert',stopindex='end',nocase=True) or self.source.search(q.get(),'1.0',stopindex='end',nocase=True)
            if pos:self.source.tag_remove('sel','1.0','end');self.source.tag_add('sel',pos,pos+f'+{len(q.get())}c');self.source.mark_set('insert',pos+f'+{len(q.get())}c');self.source.see(pos)
            else:messagebox.showinfo('查找','未找到匹配文字。',parent=win)
        def replace():
            try:
                if self.source.get('sel.first','sel.last').lower()==q.get().lower():
                    start=self.source.index('sel.first');self.source.delete('sel.first','sel.last');self.source.insert(start,r.get())
            except tk.TclError:pass
            search()
        def all_():
            if not q.get():return
            value,count=re.subn(re.escape(q.get()),lambda m:r.get(),self.content(),flags=re.I);self.source.edit_separator();self.source.configure(autoseparators=False);self.source.delete('1.0','end');self.source.insert('1.0',value);self.source.edit_separator();self.source.configure(autoseparators=True);messagebox.showinfo('替换',f'已替换 {count} 处。',parent=win)
        for col,(label,cmd) in enumerate([('下一个',search),('替换',replace),('全部替换',all_)],1):ttk.Button(win,text=label,command=cmd).grid(row=2,column=col,pady=20)
        entry.focus_set();entry.bind('<Return>',lambda e:search())
    def mode(self,mode):
        for pane in list(self.split.panes()):self.split.forget(pane)
        if mode!='read':self.split.add(self.sourceframe,weight=1)
        if mode!='edit':self.split.add(self.previewframe,weight=1)
    def zoom(self,delta):self.size=max(9,min(24,self.size+delta));self.source.config(font=('Consolas',self.size));self.preview.config(font=('Microsoft YaHei UI',self.size))
    def export(self):
        name=filedialog.asksaveasfilename(defaultextension='.html',filetypes=[('HTML 文档','*.html')])
        if name:
            try:Path(name).write_text('<!doctype html><meta charset="utf-8"><style>body{max-width:900px;margin:40px auto;font:16px/1.8 sans-serif}pre{background:#f1f4f8;padding:16px}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:8px}img{max-width:100%}</style>'+self.html(),encoding='utf-8');messagebox.showinfo('导出完成','HTML 文件已保存。')
            except OSError as e:messagebox.showerror('导出失败',str(e))
    def close(self):
        if self.confirm():self.root.destroy()
if __name__=='__main__':
    root=tk.Tk();App(root);root.mainloop()

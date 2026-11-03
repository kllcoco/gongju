import sys, os, re, json, hashlib, tempfile, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font
from html.parser import HTMLParser
from core import BASE,DATA,ASSETS
import theme
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
        self.root=root; self.path=None; self.saved=''; self.digest=None; self.encoding='utf-8'; self.job=None
        self.settings=DATA/'preferences.json'; self.prefs={}
        try: self.prefs=json.loads(self.settings.read_text('utf-8'))
        except Exception: pass
        self.recent=self.prefs.get('recent',[]); self.size=int(self.prefs.get('size',12)); self.theme_name=self.prefs.get('theme','light') if self.prefs.get('theme') in theme.PALETTES else 'light'
        self.p=theme.PALETTES[self.theme_name]; self.fonts=theme.fonts(root); self.editor_font=self.prefs.get('editor_font','mono' if self.fonts['mono_has_cjk'] else 'ui')
        root.title('墨写 · Markdown'); root.geometry('1240x800'); root.minsize(820,540)
        theme.style_ttk(root,self.p,self.fonts)
        menu=tk.Menu(root); root.config(menu=menu); self.menubar=menu
        self.filemenu=tk.Menu(menu,tearoff=False); menu.add_cascade(label='文件',menu=self.filemenu)
        for title,cmd,shortcut in [('新建',self.new,'Ctrl+N'),('打开…',self.open,'Ctrl+O'),('保存',self.save,'Ctrl+S'),('另存为…',lambda:self.save(True),'Ctrl+Shift+S'),('导出 HTML…',self.export,''),('退出',self.close,'')]: self.filemenu.add_command(label=title,command=cmd,accelerator=shortcut)
        self.recentmenu=tk.Menu(self.filemenu,tearoff=False); self.filemenu.insert_cascade(2,label='最近打开',menu=self.recentmenu); self.refresh_recent()
        edit=tk.Menu(menu,tearoff=False); menu.add_cascade(label='编辑',menu=edit)
        for label,cmd,shortcut in [('撤销',lambda:self.event('<<Undo>>'),'Ctrl+Z'),('重做',lambda:self.event('<<Redo>>'),'Ctrl+Y'),('剪切',lambda:self.event('<<Cut>>'),'Ctrl+X'),('复制',lambda:self.event('<<Copy>>'),'Ctrl+C'),('粘贴',lambda:self.event('<<Paste>>'),'Ctrl+V'),('全选',self.select_all,'Ctrl+A'),('查找 / 替换',self.find,'Ctrl+F')]: edit.add_command(label=label,command=cmd,accelerator=shortcut)
        view=tk.Menu(menu,tearoff=False); menu.add_cascade(label='视图',menu=view); self.viewmenu=view
        for label,cmd in [('编辑 + 预览',lambda:self.mode('both')),('专注编辑',lambda:self.mode('edit')),('阅读模式',lambda:self.mode('read'))]: view.add_command(label=label,command=cmd)
        view.add_separator(); view.add_command(label='放大文字',command=lambda:self.zoom(1),accelerator='Ctrl+加号'); view.add_command(label='缩小文字',command=lambda:self.zoom(-1),accelerator='Ctrl+减号')
        self.dark_var=tk.BooleanVar(value=self.theme_name=='dark'); view.add_separator(); view.add_checkbutton(label='深色模式',variable=self.dark_var,command=lambda:self.set_theme('dark' if self.dark_var.get() else 'light'))
        self.font_var=tk.StringVar(value=self.editor_font); fontmenu=tk.Menu(view,tearoff=False); view.add_cascade(label='编辑区字体',menu=fontmenu)
        fontmenu.add_radiobutton(label='等宽（'+self.fonts['mono']+'）',variable=self.font_var,value='mono',command=self.apply_fonts); fontmenu.add_radiobutton(label='中文清晰（'+self.fonts['ui']+'）',variable=self.font_var,value='ui',command=self.apply_fonts)
        helpm=tk.Menu(menu,tearoff=False);menu.add_cascade(label='帮助',menu=helpm);helpm.add_command(label='Markdown 语法指南',command=lambda:self.show_about('Markdown 语法'));helpm.add_command(label='关于与详细用法',command=self.show_about)
        self.helpmenu=helpm
        # Toolbar: brand, file name, icon buttons.
        top=ttk.Frame(root,padding=(16,8,12,8)); top.pack(fill='x'); self.topbar=top
        ttk.Label(top,text='墨写',style='Brand.TLabel').pack(side='left')
        ttk.Separator(top,orient='vertical').pack(side='left',fill='y',padx=14,pady=4)
        self.name=ttk.Label(top,text='未命名.md',style='File.TLabel'); self.name.pack(side='left')
        self.dirtymark=ttk.Label(top,text='',style='Dirty.TLabel'); self.dirtymark.pack(side='left',padx=(6,0))
        self.icons=theme.icons(root,self.p); self.toolbuttons={}
        for key,label,cmd in [('theme','',self.toggle_theme),('layout','',self.cycle_mode),('find','查找',self.find),('save','保存',self.save),('open','打开',self.open),('new','新建',self.new)]:
            b=ttk.Button(top,text=label,style='Tool.TButton',compound='left',command=cmd,takefocus=False); b.pack(side='right',padx=1); self.toolbuttons[key]=b
        self.refresh_icons()
        self.outer=ttk.Panedwindow(root,orient='horizontal'); self.outer.pack(fill='both',expand=True)
        outline=ttk.Frame(self.outer,width=210,style='Panel.TFrame'); self.outlineframe=outline
        ttk.Label(outline,text='大纲',style='PanelHead.TLabel',padding=(18,14,10,6)).pack(anchor='w')
        self.tree=ttk.Treeview(outline,show='tree',selectmode='browse'); self.tree.pack(fill='both',expand=True,padx=(8,0)); self.tree.bind('<<TreeviewSelect>>',self.jump); self.outer.add(outline,weight=0)
        self.split=ttk.Panedwindow(self.outer,orient='horizontal'); self.outer.add(self.split,weight=1)
        self.sourceframe,self.previewframe=ttk.Frame(self.split,style='Surface.TFrame'),ttk.Frame(self.split,style='Surface.TFrame')
        self.source=self.textpane(self.sourceframe,'编辑',True); self.preview=self.textpane(self.previewframe,'预览',False)
        self.split.add(self.sourceframe,weight=1); self.split.add(self.previewframe,weight=1)
        bar=ttk.Frame(root,style='Status.TFrame',padding=(18,5)); bar.pack(fill='x',side='bottom'); bar.pack_configure(before=self.outer)
        self.status=ttk.Label(bar,style='Status.TLabel'); self.status.pack(side='left'); self.status_right=ttk.Label(bar,style='Status.TLabel'); self.status_right.pack(side='right')
        self.apply_text_theme(); self.current_mode='both'
        self.source.bind('<<Modified>>',self.changed); self.source.bind('<KeyRelease>',lambda e:self.update_status()); self.source.bind('<ButtonRelease-1>',lambda e:self.update_status()); self.source.bind('<Tab>',self.tab); self.source.bind('<Shift-Tab>',self.untab); self.source.bind('<ISO_Left_Tab>',self.untab); self.source.bind('<Control-y>',lambda e:(self.event('<<Redo>>'),'break')[1])
        for key,cmd in [('n',self.new),('o',self.open),('s',self.save),('f',self.find),('h',self.find),('b',lambda:self.insert('**','**')),('i',lambda:self.insert('*','*'))]:
            root.bind('<Control-'+key+'>',lambda e,c=cmd:(c(),'break')[1])
        root.bind('<Control-Shift-S>',lambda e:self.save(True)); root.protocol('WM_DELETE_WINDOW',self.close)
        self.render(); self.source.focus_set()
        if len(sys.argv)>1: self.load(Path(sys.argv[1]))
    def textpane(self,frame,label,editable):
        ttk.Label(frame,text=label,style='Head.TLabel',padding=(30,12,10,0)).pack(anchor='w'); inner=ttk.Frame(frame,style='Surface.TFrame'); inner.pack(fill='both',expand=True)
        text=tk.Text(inner,wrap='word',undo=editable,maxundo=200,autoseparators=True)
        scroll=ttk.Scrollbar(inner,command=text.yview); scroll.pack(side='right',fill='y'); text.configure(yscrollcommand=scroll.set); text.pack(fill='both',expand=True); return text
    # ---- appearance -------------------------------------------------------
    def save_prefs(self):
        self.prefs.update({'recent':self.recent,'theme':self.theme_name,'size':self.size,'editor_font':self.editor_font})
        try:self.settings.write_text(json.dumps(self.prefs,ensure_ascii=False),encoding='utf-8')
        except OSError:pass
    def editor_family(self):return self.fonts['mono'] if self.editor_font=='mono' else self.fonts['ui']
    def apply_text_theme(self):
        p=self.p;ui=self.fonts['ui'];size=self.size
        self.source.configure(**theme.text_options(p,self.editor_family(),size))
        self.preview.configure(**theme.text_options(p,ui,size+1,False));self.preview.configure(padx=36,spacing1=4,spacing2=4,spacing3=6)
        for i,(delta,above) in enumerate([(12,18),(8,16),(5,14),(3,12),(2,10),(1,10)],1):
            self.preview.tag_configure('h'+str(i),font=(ui,size+delta,'bold'),spacing1=above,spacing3=8,foreground=p['text'] if i<4 else p['muted'] if i==6 else p['text'])
        mono=self.fonts['mono']
        styles={'bold':{'font':(ui,size+1,'bold')},'italic':{'font':(ui,size+1,'italic')},'del':{'overstrike':True,'foreground':p['muted']},
                'code':{'font':(mono,size),'background':p['code_bg'],'foreground':p['code_fg']},
                'pre':{'font':(mono,size),'background':p['code_bg'],'foreground':p['text'],'lmargin1':18,'lmargin2':18,'rmargin':18,'spacing1':0,'spacing2':1,'spacing3':0},
                'quote':{'foreground':p['muted'],'lmargin1':22,'lmargin2':22,'background':p['quote_bg']},'muted':{'foreground':p['muted']},'prepad':{'font':(mono,3)},'gap':{'font':(mono,4),'spacing1':0,'spacing3':0},'p':{'spacing3':12},'li':{'spacing3':4},'marker':{'foreground':p['accent']},
                'outline_target':{'background':p['accent_soft']}}
        for name,options in styles.items():self.preview.tag_configure(name,**options)
        try:self.preview.tag_configure('quote',lmargincolor=p['quote_bar'])
        except tk.TclError:pass
        for d in range(1,7):self.preview.tag_configure('indent'+str(d),lmargin1=8+(d-1)*24,lmargin2=8+d*24)
        self.preview.tag_raise('code');self.preview.tag_raise('pre')
        for name,opts in {'md_heading':{'foreground':p['accent'],'font':(self.editor_family(),size,'bold')},'md_code':{'foreground':p['code_fg']},'md_codeblock':{'background':p['code_bg']},'md_quote':{'foreground':p['muted']},'md_list':{'foreground':p['accent']},'curline':{'background':p['curline']},'outline_target':{'background':p['accent_soft']}}.items():
            self.source.tag_configure(name,**opts)
        self.source.tag_lower('curline');self.source.tag_raise('sel')
        self.tree.tag_configure('lvl1',font=(ui,10,'bold'),foreground=p['text']);self.tree.tag_configure('lvl2',foreground=p['text'])
        for i in range(3,7):self.tree.tag_configure('lvl'+str(i),foreground=p['muted'])
        self.tree.tag_configure('current',foreground=p['accent'])
        for m in [self.menubar,getattr(self,'context_menu',None)]:
            if m is not None:theme.recolor_menu(m,p)
    def refresh_icons(self):
        self.icons=theme.icons(self.root,self.p)
        for key,b in self.toolbuttons.items():
            icon={'theme':'sun' if self.theme_name=='dark' else 'moon'}.get(key,key);b.configure(image=self.icons[icon])
    def set_theme(self,name):
        self.theme_name=name;self.p=theme.PALETTES[name];self.dark_var.set(name=='dark')
        theme.style_ttk(self.root,self.p,self.fonts);self.refresh_icons();self.apply_text_theme();self.save_prefs();self.render()
        for w in self.root.winfo_children():
            if isinstance(w,tk.Toplevel):
                try:w.configure(bg=self.p['bg'])
                except tk.TclError:pass
    def toggle_theme(self):self.set_theme('light' if self.theme_name=='dark' else 'dark')
    def apply_fonts(self):self.editor_font=self.font_var.get();self.apply_text_theme();self.save_prefs();self.render()
    def cycle_mode(self):self.mode({'both':'edit','edit':'read','read':'both'}[self.current_mode])
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
        # Large documents re-render less eagerly so typing stays responsive.
        size=int(self.source.index('end-1c').split('.')[0])
        self.job=self.root.after(220 if size<800 else 450 if size<3000 else 900,self.render); self.update_status()
    def update_status(self):
        text=self.content(); name=self.path.name if self.path else '未命名.md'; dirty=self.dirty()
        self.name.config(text=name); self.dirtymark.config(text='● 未保存' if dirty else '')
        self.root.title(('* ' if dirty else '')+name+' — 墨写 Markdown')
        line,col=self.source.index('insert').split('.')
        words=len(re.findall(r'[\u3400-\u9fff\uf900-\ufaff]|[A-Za-z0-9_]+(?:[\'’][A-Za-z]+)?',text))
        self.status.config(text=f'{words:,} 字   ·   第 {line} 行，第 {int(col)+1} 列   ·   {self.encoding.upper()}')
        self.status_right.config(text=str(self.path) if self.path else '尚未保存到磁盘')
        self.source.tag_remove('curline','1.0','end'); self.source.tag_add('curline','insert linestart','insert lineend+1c')
        self.mark_current_heading(int(line))
    def mark_current_heading(self,line):
        current=None
        for key in self.tree.get_children():
            if int(key)<=line:current=key
            self.tree.item(key,tags=tuple(t for t in self.tree.item(key,'tags') if t!='current'))
        if current:self.tree.item(current,tags=tuple(self.tree.item(current,'tags'))+('current',))
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
        self.save_prefs();self.refresh_recent()
    def refresh_recent(self):
        self.recentmenu.delete(0,'end')
        for p in self.recent:self.recentmenu.add_command(label=p,command=lambda p=p:self.load(Path(p)) if self.confirm() else None)
    def insert(self,before,after='',line=False):
        self.source.focus_set(); self.source.edit_separator(); self.source.configure(autoseparators=False)
        try:start,end=self.source.index('sel.first'),self.source.index('sel.last');text=self.source.get(start,end)
        except tk.TclError:start=end=self.source.index('insert');text=''
        if line:start=self.source.index(start+' linestart')
        self.source.delete(start,end);self.source.insert(start,before+text+after);self.source.edit_separator();self.source.configure(autoseparators=True)
    def tab(self,e):
        try:first,last=self.source.index('sel.first'),self.source.index('sel.last')
        except tk.TclError:self.insert('    ');return 'break'
        if first.split('.')[0]==last.split('.')[0]:self.insert('    ');return 'break'
        self.shift_lines(1);return 'break'
    def untab(self,e):self.shift_lines(-1);return 'break'
    def shift_lines(self,direction):
        try:first,last=self.source.index('sel.first'),self.source.index('sel.last')
        except tk.TclError:first=last=self.source.index('insert')
        a=int(first.split('.')[0]);b=int(last.split('.')[0])
        if b>a and last.endswith('.0'):b-=1
        start,end=f'{a}.0',f'{b}.end';lines=self.source.get(start,end).split('\n')
        lines=['    '+l for l in lines] if direction>0 else [re.sub(r'^( {1,4}|\t)','',l) for l in lines]
        self.source.edit_separator();self.source.configure(autoseparators=False)
        self.source.delete(start,end);self.source.insert(start,'\n'.join(lines));self.source.tag_add('sel',start,f'{b}.end')
        self.source.edit_separator();self.source.configure(autoseparators=True)
    def image(self):
        name=filedialog.askopenfilename(filetypes=[('图片','*.png *.jpg *.jpeg *.gif *.webp')])
        if name:
            path=os.path.relpath(name,self.path.parent) if self.path else name
            self.insert('![图片]('+path.replace('\\','/')+')')
    def find(self):
        old=getattr(self,'find_window',None)
        if old is not None and old.winfo_exists():old.deiconify();old.lift();old.focus_entry();return
        win=tk.Toplevel(self.root);win.title('查找与替换');win.geometry('480x200');win.resizable(False,False);win.transient(self.root);win.configure(bg=self.p['bg']);self.find_window=win
        box=ttk.Frame(win,padding=(20,18,20,10));box.pack(fill='both',expand=True);box.columnconfigure(1,weight=1)
        q=tk.StringVar();r=tk.StringVar()
        try:q.set(self.source.get('sel.first','sel.last').split('\n')[0])
        except tk.TclError:pass
        ttk.Label(box,text='查找').grid(row=0,column=0,sticky='w',padx=(0,12),pady=6);entry=ttk.Entry(box,textvariable=q);entry.grid(row=0,column=1,sticky='ew',pady=6)
        ttk.Label(box,text='替换为').grid(row=1,column=0,sticky='w',padx=(0,12),pady=6);ttk.Entry(box,textvariable=r).grid(row=1,column=1,sticky='ew',pady=6)
        buttons=ttk.Frame(box);buttons.grid(row=2,column=0,columnspan=2,sticky='e',pady=(14,0))
        win.focus_entry=lambda:(entry.focus_set(),entry.select_range(0,'end'))
        win.bind('<Escape>',lambda e:win.destroy())
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
        for label,cmd,style in [('全部替换',all_,'TButton'),('替换',replace,'TButton'),('下一个',search,'Accent.TButton')]:ttk.Button(buttons,text=label,command=cmd,style=style).pack(side='left',padx=(8,0))
        win.focus_entry();entry.bind('<Return>',lambda e:search())
    def mode(self,mode):
        self.current_mode=mode
        for pane in list(self.split.panes()):self.split.forget(pane)
        if mode!='read':self.split.add(self.sourceframe,weight=1)
        if mode!='edit':self.split.add(self.previewframe,weight=1)
    def zoom(self,delta):
        self.size=max(9,min(24,self.size+delta));self.apply_text_theme();self.save_prefs();self.render()
    def export(self):
        name=filedialog.asksaveasfilename(defaultextension='.html',filetypes=[('HTML 文档','*.html')])
        if name:
            try:Path(name).write_text('<!doctype html><meta charset="utf-8"><style>body{max-width:900px;margin:40px auto;font:16px/1.8 sans-serif}pre{background:#f1f4f8;padding:16px}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:8px}img{max-width:100%}</style>'+self.html(),encoding='utf-8');messagebox.showinfo('导出完成','HTML 文件已保存。')
            except OSError as e:messagebox.showerror('导出失败',str(e))
    def close(self):
        if self.confirm():self.root.destroy()
if __name__=='__main__':
    root=tk.Tk();App(root);root.mainloop()

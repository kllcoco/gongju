"""Searchable, modeless About/help window with copyable templates."""
import tkinter as tk
from tkinter import ttk
from help_content import TOPICS,VERSION

class UsageGuide:
    def __init__(self,app):
        self.app=app;self.topic=None;self.timer=None
        self.window=tk.Toplevel(app.root);win=self.window
        win.title(f'关于墨写 {VERSION} · 详细用法与模板');win.configure(bg=getattr(app,'p',{}).get('bg','#f6f7f9'));win.geometry('1100x780');win.minsize(820,600);win.transient(app.root)
        header=ttk.Frame(win,padding=(18,14));header.pack(fill='x')
        ttk.Label(header,text=f'墨写 {VERSION}  使用指南',style='Brand.TLabel').pack(anchor='w')
        ttk.Label(header,text='选择功能查看步骤，复制正确模板；本窗口不会改动当前文档。',style='Muted.TLabel').pack(anchor='w',pady=(5,10))
        searchbar=ttk.Frame(header);searchbar.pack(fill='x')
        ttk.Label(searchbar,text='搜索功能：').pack(side='left');self.query=tk.StringVar()
        self.search=ttk.Entry(searchbar,textvariable=self.query);self.search.pack(side='left',fill='x',expand=True)
        ttk.Button(searchbar,text='清空',command=lambda:self.query.set('')).pack(side='left',padx=(8,0))
        self.query.trace_add('write',self.schedule_filter)
        split=ttk.Panedwindow(win,orient='horizontal');split.pack(fill='both',expand=True,padx=18)
        left=ttk.Frame(split,width=250);split.add(left,weight=0)
        self.tree=ttk.Treeview(left,show='tree',selectmode='browse',height=20);ys=ttk.Scrollbar(left,orient='vertical',command=self.tree.yview);self.tree.config(yscrollcommand=ys.set);ys.pack(side='right',fill='y');self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',self.selected)
        right=ttk.Frame(split);split.add(right,weight=1)
        self.heading=ttk.Label(right,text='',font=(getattr(app,'fonts',{}).get('ui','Microsoft YaHei UI'),14,'bold'),padding=(14,6));self.heading.pack(fill='x')
        panes=ttk.Panedwindow(right,orient='vertical');panes.pack(fill='both',expand=True)
        bodyframe=ttk.Frame(panes);panes.add(bodyframe,weight=1);self.body=self.textpane(bodyframe)
        templateframe=ttk.Frame(panes);panes.add(templateframe,weight=2)
        self.template_label=ttk.Label(templateframe,text='可复制模板',padding=(14,5));self.template_label.pack(anchor='w')
        self.template=self.textpane(templateframe,code=True)
        footer=ttk.Frame(win,padding=(18,12));footer.pack(fill='x')
        self.copy_button=ttk.Button(footer,text='复制模板',command=self.copy_template);self.copy_button.pack(side='left')
        self.section_button=ttk.Button(footer,text='复制本节说明',command=self.copy_section);self.section_button.pack(side='left',padx=6)
        self.new_button=ttk.Button(footer,text='用模板新建文档',command=self.new_document);self.new_button.pack(side='left')
        ttk.Button(footer,text='关闭',command=win.destroy).pack(side='right')
        self.status=ttk.Label(win,padding=(18,0,18,12));self.status.pack(fill='x')
        win.bind('<Escape>',lambda e:win.destroy());win.bind('<Control-f>',lambda e:(self.search.focus_set(),'break')[1])
        self.filter_topics();self.search.focus_set()
    def textpane(self,parent,code=False):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        p=getattr(self.app,'p',None) or {'code_bg':'#f3f5f7','surface':'#ffffff','text':'#1f2933','sel':'#c9ebe3'}
        fonts=getattr(self.app,'fonts',{'ui':'Microsoft YaHei UI','mono':'Consolas'})
        text=tk.Text(frame,wrap='word',font=(fonts['mono'] if code else fonts['ui'],11),bg=p['code_bg'] if code else p['surface'],fg=p['text'],selectbackground=p['sel'],relief='flat',borderwidth=0,highlightthickness=0,padx=16,pady=12,spacing3=7,state='disabled',exportselection=False)
        scroll=ttk.Scrollbar(frame,command=text.yview);scroll.pack(side='right',fill='y');text.config(yscrollcommand=scroll.set);text.pack(fill='both',expand=True)
        text.bind('<Control-a>',lambda e:(text.tag_add('sel','1.0','end-1c'),'break')[1])
        return text
    def schedule_filter(self,*args):
        if self.timer:self.window.after_cancel(self.timer)
        self.timer=self.window.after(120,self.filter_topics)
    def filter_topics(self):
        self.timer=None;previous=self.topic['id'] if self.topic else None;query=self.query.get().strip().casefold()
        matches=[t for t in TOPICS if query in (' '.join([t['category'],t['title'],t['steps'],t['template']])).casefold()]
        self.tree.delete(*self.tree.get_children());groups={}
        for topic in matches:
            category=topic['category']
            if category not in groups:
                key='category-'+str(len(groups));groups[category]=key;self.tree.insert('','end',iid=key,text=category,open=True)
            self.tree.insert(groups[category],'end',iid='topic-'+topic['id'],text=topic['title'])
        if matches:
            chosen=next((t for t in matches if t['id']==previous),matches[0]);self.tree.selection_set('topic-'+chosen['id']);self.tree.see('topic-'+chosen['id']);self.show(chosen)
        else:
            self.topic=None;self.heading.config(text='没有找到匹配的功能');self.put(self.body,'请缩短关键词，例如：保存、图片、脚注、MCP。');self.put(self.template,'');self.buttons(False,False)
        self.status.config(text=f'找到 {len(matches)} 项功能 · Ctrl+F 搜索 · Esc 关闭')
    def selected(self,event=None):
        keys=self.tree.selection()
        if keys and keys[0].startswith('topic-'):
            key=keys[0][6:];self.show(next(t for t in TOPICS if t['id']==key))
    def put(self,widget,text):
        widget.config(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.config(state='disabled');widget.yview_moveto(0)
    def buttons(self,has_topic,has_template):
        self.section_button.config(state='normal' if has_topic else 'disabled');self.copy_button.config(state='normal' if has_template else 'disabled')
        self.new_button.config(state='normal' if has_template and self.topic['language']=='markdown' else 'disabled')
    def show(self,topic):
        self.topic=topic;self.heading.config(text=topic['title']);self.put(self.body,topic['steps']);self.put(self.template,topic['template'])
        label={'markdown':'Markdown 模板 · 可复制到编辑区','json':'JSON 模板 · 先阅读上方参数说明','text':'操作示例 / 可复制指令'}[topic['language']]
        self.template_label.config(text=label);self.buttons(True,bool(topic['template']))
    def copy(self,text):
        self.window.clipboard_clear();self.window.clipboard_append(text);self.status.config(text='已复制，可以粘贴使用。需要填写的路径或参数请按本节说明替换。')
    def copy_template(self):
        if self.topic:self.copy(self.topic['template'])
    def copy_section(self):
        if self.topic:self.copy(self.topic['title']+'\n\n'+self.topic['steps']+'\n\n'+self.topic['template'])
    def new_document(self):
        if not self.topic or self.topic['language']!='markdown' or not self.app.confirm():return
        self.app.path=None;self.app.digest=None;self.app.encoding='utf-8';self.app.settext('');self.app.source.insert('1.0',self.topic['template']);self.app.source.mark_set('insert','1.0');self.app.render();self.app.source.focus_set();self.window.destroy()

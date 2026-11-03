import re
import tkinter as tk
from tkinter import ttk
import markdown
from writing_extension import WritingExtension

class SyntaxTools:
    def html(self):return markdown.markdown(self.content(),extensions=['extra','sane_lists','nl2br',WritingExtension()])
    def build_toolbar(self,bar):
        self.syntax_commands={
            '粗体':lambda:self.wrap('**','**','粗体文字'),
            '斜体':lambda:self.wrap('*','*','斜体文字'),
            '粗斜体':lambda:self.wrap('***','***','强调文字'),
            '删除线':lambda:self.wrap('~~','~~','删除文字'),
            '行内代码':self.inline_code,
            '无序列表':lambda:self.lines('unordered'),
            '有序列表':lambda:self.lines('ordered'),
            '任务清单':lambda:self.lines('task'),
            '已完成任务':lambda:self.lines('done'),
            '引用':lambda:self.lines('quote'),
            '嵌套引用':lambda:self.lines('nested'),
            '链接':lambda:self.wrap('[','](https://example.com)','链接文字'),
            '图片':self.image,
            '图片语法':lambda:self.wrap('![','](图片路径.png)','图片说明'),
            '表格':lambda:self.block('| 列一 | 列二 |\n| --- | --- |\n| 内容 | 内容 |'),
            '分隔线':lambda:self.block('---'),
            '脚注':self.footnote,
            '引用式链接':self.reference_link,
            '定义列表':lambda:self.block('术语\n: 术语的解释'),
            '强制换行':lambda:self.wrap('  \n','',''),
            '转义特殊字符':self.escape_selection,
        }
        heading=ttk.Menubutton(bar,text='标题 ▾',width=6);headmenu=tk.Menu(heading,tearoff=False)
        for level in range(1,7):headmenu.add_command(label=f'H{level} · {level} 级标题',command=lambda n=level:self.heading(n))
        headmenu.add_separator();headmenu.add_command(label='恢复正文',command=lambda:self.heading(0));heading.config(menu=headmenu);heading.pack(side='left',padx=2)
        self.heading_menu=headmenu
        for label,key in [('粗体','粗体'),('斜体','斜体'),('代码','行内代码')]:ttk.Button(bar,text=label,width=5,command=self.syntax_commands[key]).pack(side='left',padx=2)
        lists=ttk.Menubutton(bar,text='列表 ▾',width=6);listmenu=tk.Menu(lists,tearoff=False)
        for key in ['无序列表','有序列表','任务清单','已完成任务']:listmenu.add_command(label=key,command=self.syntax_commands[key])
        lists.config(menu=listmenu);lists.pack(side='left',padx=2)
        for key in ['引用','链接','图片','表格']:ttk.Button(bar,text=key,width=5,command=self.syntax_commands[key]).pack(side='left',padx=2)
        more=ttk.Menubutton(bar,text='更多语法 ▾',width=10);self.more_menu=self.syntax_menu(more);more.config(menu=self.more_menu);more.pack(side='left',padx=(8,2))
        ttk.Button(bar,text='查找',width=5,command=self.find).pack(side='right',padx=2)
    def syntax_menu(self,parent):
        menu=tk.Menu(parent,tearoff=False)
        headings=tk.Menu(menu,tearoff=False)
        for level in range(1,7):headings.add_command(label=f'H{level} · {level} 级标题',command=lambda n=level:self.heading(n))
        headings.add_separator();headings.add_command(label='恢复正文',command=lambda:self.heading(0));menu.add_cascade(label='标题',menu=headings)
        groups=[('文字格式',['粗体','斜体','粗斜体','删除线','行内代码']),('列表与任务',['无序列表','有序列表','任务清单','已完成任务']),('引用',['引用','嵌套引用']),('链接与图片',['链接','图片','图片语法','引用式链接'])]
        for title,keys in groups:
            child=tk.Menu(menu,tearoff=False)
            for key in keys:child.add_command(label=key,command=self.syntax_commands[key])
            menu.add_cascade(label=title,menu=child)
        code=tk.Menu(menu,tearoff=False)
        for title,lang in [('普通代码块',''),('Python','python'),('JavaScript','javascript'),('TypeScript','typescript'),('JSON','json'),('Shell','bash'),('SQL','sql'),('HTML','html'),('CSS','css')]:code.add_command(label=title,command=lambda lang=lang:self.code_block(lang))
        menu.add_cascade(label='代码块',menu=code);menu.add_separator()
        for key in ['表格','分隔线','脚注','定义列表','强制换行','转义特殊字符']:menu.add_command(label=key,command=self.syntax_commands[key])
        return menu
    def setup_syntax(self):
        self.source.configure(exportselection=False)
        self.context_menu=tk.Menu(self.source,tearoff=False)
        for title,event in [('撤销','<<Undo>>'),('重做','<<Redo>>'),('剪切','<<Cut>>'),('复制','<<Copy>>'),('粘贴','<<Paste>>')]:self.context_menu.add_command(label=title,command=lambda ev=event:self.event(ev))
        self.context_menu.add_command(label='全选',command=self.select_all);self.context_menu.add_separator()
        # All insertion categories are available directly in the right-click menu.
        insert_menu=self.syntax_menu(self.source)
        for index in range(insert_menu.index('end')+1):
            kind=insert_menu.type(index)
            if kind=='separator':self.context_menu.add_separator()
            elif kind=='cascade':self.context_menu.add_cascade(label=insert_menu.entrycget(index,'label'),menu=insert_menu.entrycget(index,'menu'))
            else:self.context_menu.add_command(label=insert_menu.entrycget(index,'label'),command=insert_menu.entrycget(index,'command'))
        self._context_definitions=insert_menu
        self.source.bind('<Button-3>',self.popup_syntax)
        self.source.bind('<Shift-F10>',self.popup_syntax_keyboard)
        self.root.bind('<Control-b>',lambda e:(self.syntax_commands['粗体'](),'break')[1]);self.root.bind('<Control-i>',lambda e:(self.syntax_commands['斜体'](),'break')[1])
        self.preview.tag_configure('del',overstrike=True)
    def popup_syntax(self,event):
        # Preserve a selection for formatting, otherwise move the caret to the clicked position.
        if not self.source.tag_ranges('sel'):self.source.mark_set('insert',self.source.index(f'@{event.x},{event.y}'))
        self.source.focus_set()
        try:self.context_menu.tk_popup(event.x_root,event.y_root)
        finally:self.context_menu.grab_release()
        return 'break'
    def popup_syntax_keyboard(self,event):
        box=self.source.bbox('insert') or (10,10,0,20)
        try:self.context_menu.tk_popup(self.source.winfo_rootx()+box[0],self.source.winfo_rooty()+box[1]+box[3])
        finally:self.context_menu.grab_release()
        return 'break'
    def selection(self):
        try:start,end=self.source.index('sel.first'),self.source.index('sel.last')
        except tk.TclError:start=end=self.source.index('insert')
        return start,end,self.source.get(start,end)
    def replace_range(self,start,end,text,select=None):
        self.source.edit_separator();self.source.configure(autoseparators=False)
        try:
            self.source.delete(start,end);self.source.insert(start,text);self.source.tag_remove('sel','1.0','end')
            self.source.mark_set('insert',f'{start}+{len(text)}c')
            if select:
                a,b=select;self.source.tag_add('sel',f'{start}+{a}c',f'{start}+{b}c');self.source.mark_set('insert',f'{start}+{b}c')
        finally:self.source.configure(autoseparators=True);self.source.edit_separator()
        self.source.focus_set();self.source.see('insert')
    def wrap(self,before,after,placeholder):
        start,end,text=self.selection();text=text or placeholder
        self.replace_range(start,end,before+text+after,(len(before),len(before)+len(text)) if text else None)
    def line_selection(self):
        start,end,text=self.selection();start=self.source.index(start+' linestart')
        if end.endswith('.0') and self.source.compare(end,'>',start):end=self.source.index(end+'-1c')
        end=self.source.index(end+' lineend');return start,end,self.source.get(start,end)
    def heading(self,level):
        start,end,text=self.line_selection();out=[]
        for line in text.split('\n'):
            line=re.sub(r'^ {0,3}#{1,6}(?:\s+|$)','',line);line=re.sub(r'\s+#+\s*$','',line)
            out.append(('#'*level+' ' if level else '')+(line or ('标题' if level else '')))
        self.replace_range(start,end,'\n'.join(out))
    def lines(self,kind):
        start,end,text=self.line_selection();out=[]
        for n,line in enumerate(text.split('\n'),1):
            line=re.sub(r'^\s*(?:[-*+] |\d+[.)] )(?:\[[ xX]\] )?','',line)
            if kind in ('quote','nested'):line=re.sub(r'^\s*(?:>\s*)+','',line)
            prefix={'unordered':'- ','ordered':f'{n}. ','task':'- [ ] ','done':'- [x] ','quote':'> ','nested':'> > '}[kind]
            out.append(prefix+(line or ('引用内容' if 'quote'==kind or kind=='nested' else '列表项目')))
        self.replace_range(start,end,'\n'.join(out))
    def block(self,text):
        start,end,selected=self.selection();before=self.source.get('1.0',start);after=self.source.get(end,'end-1c')
        prefix='\n'*(max(0,2-(len(before)-len(before.rstrip('\n'))))) if before else ''
        suffix='\n'*(max(0,2-(len(after)-len(after.lstrip('\n'))))) if after else '\n'
        self.replace_range(start,end,prefix+text+suffix)
    def inline_code(self):
        start,end,text=self.selection();text=text or '代码';runs=re.findall(r'`+',text);fence='`'*(max(map(len,runs),default=0)+1);pad=' ' if text.startswith('`') or text.endswith('`') else ''
        self.replace_range(start,end,fence+pad+text+pad+fence,(len(fence+pad),len(fence+pad)+len(text)))
    def code_block(self,language=''):
        _,_,text=self.selection();text=text or '在这里输入代码';fence='`'*max(3,max(map(len,re.findall(r'`+',text)),default=0)+1)
        self.block(fence+language+'\n'+text+'\n'+fence)
    def unique_label(self,prefix):
        n=1
        while re.search(r'\[\^?'+re.escape(prefix+str(n))+r'\]',self.content()):n+=1
        return prefix+str(n)
    def footnote(self):
        start,end,text=self.selection();label=self.unique_label('note');self.source.edit_separator();self.source.configure(autoseparators=False)
        try:
            self.source.delete(start,end);self.source.insert(start,text+'[^'+label+']')
            self.source.insert('end','\n\n[^'+label+']: 脚注内容\n');self.source.tag_remove('sel','1.0','end');self.source.mark_set('insert','end-1c')
        finally:self.source.configure(autoseparators=True);self.source.edit_separator()
        self.source.focus_set();self.source.see('insert')
    def reference_link(self):
        start,end,text=self.selection();label=self.unique_label('link');self.source.edit_separator();self.source.configure(autoseparators=False)
        try:
            self.source.delete(start,end);self.source.insert(start,'['+(text or '链接文字')+']['+label+']');self.source.insert('end','\n\n['+label+']: https://example.com\n')
        finally:self.source.configure(autoseparators=True);self.source.edit_separator()
        self.source.focus_set()
    def escape_selection(self):
        start,end,text=self.selection();text=text or '*文字*';self.replace_range(start,end,re.sub(r'([\\`*_{}\[\]()#+\-.!|>~])',r'\\\1',text))

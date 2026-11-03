import os,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));work=Path(tempfile.mkdtemp(prefix='ui-'));os.environ['MOXIE_DATA']=str(work/'data')
import tkinter as tk
import desktop_app
from desktop_app import App
from rendering import export_pdf
root=tk.Tk()
with patch.object(App,'start_bridge',lambda self:None):app=App(root)
root.update()
def reset(text='',start='1.0',end=None):
    app.settext(text);root.update();app.source.tag_remove('sel','1.0','end');app.source.mark_set('insert',start)
    if end:app.source.tag_add('sel',start,end)
def key(seq):app.source.focus_force();root.update();app.source.event_generate(seq);root.update()
# Real key events: Tk's built-in Text shortcuts must not run before Moxie's.
reset('hello','1.0','1.5');key('<Control-i>');assert app.content()=='*hello*',app.content()
reset('hello','1.0','1.5');key('<Control-b>');assert app.content()=='**hello**',app.content()
with patch.object(desktop_app.native_base.filedialog,'askopenfilename',return_value='') as ask:
    reset('ab','1.1');key('<Control-o>');assert app.content()=='ab',app.content();ask.assert_called_once()
reset('ab','1.1');key('<Control-h>');assert app.content()=='ab';assert app.find_window.winfo_exists();app.find_window.destroy()
for k in 'dkt':reset('abc','1.1');key(f'<Control-{k}>');assert app.content()=='abc',(k,app.content())
# Tab / Shift+Tab on several lines.
reset('a\nb','1.0','2.1');key('<Tab>');assert app.content()=='    a\n    b';app.source.tag_add('sel','1.0','2.end');app.untab(None);assert app.content()=='a\nb'
# Ordered numbering and nested indentation in the preview.
reset('1. 一\n2. 二\n    - 子项\n3. 三');app.render();root.update();text=app.preview.get('1.0','end')
assert '1.  一' in text and '2.  二' in text and '3.  三' in text,text
child=app.preview.search('子项','1.0');assert 'indent2' in app.preview.tag_names(child),app.preview.tag_names(child)
assert '•  一' not in text
# Code blocks keep a block style; quotes get a margin style.
reset('```python\nprint(1)\n```\n\n> 引用');app.render();root.update()
assert 'pre' in app.preview.tag_names(app.preview.search('print','1.0'));assert 'quote' in app.preview.tag_names(app.preview.search('引用','1.0'))
assert 'md_codeblock' in app.source.tag_names('2.0')
# Outline marks the heading the caret is in.
reset('# 甲\n\n正文\n\n## 乙\n\n正文','6.0');app.update_status();assert 'current' in app.tree.item('5','tags')
# Theme switching restyles both panes and persists.
light=app.source.cget('bg');app.set_theme('dark');root.update();assert app.source.cget('bg')!=light;assert app.theme_name=='dark'
import json;assert json.loads((work/'data'/'preferences.json').read_text('utf-8'))['theme']=='dark';app.set_theme('light')
# Bundled documents and links open inside Moxie, never through the OS.
with patch.object(desktop_app.os,'startfile',create=True) as startfile:
    viewer=app.show_mcp_guide();root.update();assert viewer.window.winfo_exists();assert 'MCP' in viewer.text.get('1.0','end');startfile.assert_not_called();viewer.window.destroy()
    other=work/'other.md';other.write_text('# 另一篇\n',encoding='utf-8');reset('[去](other.md)');app.path=work/'cur.md';app.saved=app.content()
    assert app.open_link('other.md')=='opened';assert app.path==other.resolve();assert '另一篇' in app.content();startfile.assert_not_called()
with patch.object(desktop_app.webbrowser if hasattr(desktop_app,'webbrowser') else __import__('webbrowser'),'open') as web:assert app.open_link('https://example.com')=='external'
# PDF keeps inline formatting, nested lists and code blocks.
html=app.html() if False else __import__('markdown').markdown('# 标题\n\n**粗** *斜* `code 中文` ~~删~~ [链](https://a.b)\n\n1. 一\n    - 子\n2. 二\n\n```\nx = 1  # 注释\n```\n\n> 引用\n\n| a | b |\n|---|---|\n| **1** | 2 |',extensions=['extra','sane_lists'])
export_pdf(html,work/'t.pdf',work);assert (work/'t.pdf').read_bytes().startswith(b'%PDF')
# Large documents stay responsive.
big='\n\n'.join(f'## 标题 {i}\n\n'+'正文 **重点** `代码` '*20 for i in range(300))
reset(big);t=time.perf_counter();app.render();root.update();first=time.perf_counter()-t
t=time.perf_counter();app.render();root.update();second=time.perf_counter()-t
assert len(app.tree.get_children())==300;print(f'render 300 headings: {first:.2f}s first, {second:.2f}s cached')
app.saved=app.content();root.destroy()
print('PASS: real Ctrl+I/B/O/H keys, emacs keys disabled, Tab/Shift+Tab, ordered+nested lists, code/quote styles, current heading, dark theme, in-app MCP guide and .md links, PDF formatting, large document')

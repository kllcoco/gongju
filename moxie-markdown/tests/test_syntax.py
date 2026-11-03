import os,sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));os.environ['MOXIE_DATA']=tempfile.mkdtemp(prefix='syntax-')
import tkinter as tk
from desktop_app import App
root=tk.Tk()
with patch.object(App,'start_bridge',lambda self:None):app=App(root)
root.update()
def reset(text='',start='1.0',end=None):
    app.settext(text);root.update();app.source.tag_remove('sel','1.0','end');app.source.mark_set('insert',start)
    if end:app.source.tag_add('sel',start,end)
def rendered():app.render();root.update();return app.preview.get('1.0','end')
reset('hello','1.0','1.5');app.syntax_commands['删除线']();assert app.content()=='~~hello~~';assert '<del>hello</del>' in app.html();rendered();assert app.preview.tag_ranges('del');app.source.edit_undo();assert app.content()=='hello'
reset('甲\n乙','1.0','2.1');app.syntax_commands['有序列表']();assert app.content()=='1. 甲\n2. 乙';app.lines('task');assert '甲' in app.content()
reset('甲\n乙','1.0','2.1');app.syntax_commands['任务清单']();assert app.content()=='- [ ] 甲\n- [ ] 乙';assert '<input' in app.html();assert '☐' in rendered()
reset('完成');app.syntax_commands['已完成任务']();assert 'checked' in app.html();assert '☑' in rendered()
for level in range(1,7):
    reset('## 原标题');app.heading(level);assert app.content()=='#'*level+' 原标题';assert f'<h{level}>' in app.html()
reset('## 原标题');app.heading(0);assert app.content()=='原标题'
reset('段落','1.2');app.footnote();assert '[^note1]' in app.content();app.footnote();assert '[^note2]' in app.content();assert 'footnote' in app.html()
reset('abc','1.0','1.3');app.code_block('python');assert '```python\nabc\n```' in app.content();app.source.edit_undo();assert app.content()=='abc'
reset('`literal`','1.0','1.9');app.inline_code();assert '<code>`literal`</code>' in app.html()
reset('~~literal~~','1.0','1.11');app.code_block();assert '<del>' not in app.html()
reset('正文','1.2');app.syntax_commands['分隔线']();assert '<hr' in app.html()
reset('文字','1.0','1.2');app.reference_link();assert '<a href="https://example.com">文字</a>' in app.html();app.source.edit_undo();assert app.content()=='文字'
reset('文字','1.0','1.2')
class E:x=200;y=20;x_root=200;y_root=200
with patch.object(app.context_menu,'tk_popup') as popup:
    app.popup_syntax(E());popup.assert_called_once();assert app.source.get('sel.first','sel.last')=='文字'
app.source.tag_remove('sel','1.0','end')
with patch.object(app.context_menu,'tk_popup'):app.popup_syntax(E());assert app.source.index('insert')==app.source.index('@200,20')
labels=[app.context_menu.entrycget(i,'label') for i in range(app.context_menu.index('end')+1) if app.context_menu.type(i)!='separator']
assert all(x in labels for x in ['标题','文字格式','列表与任务','引用','链接与图片','代码块','表格','分隔线','脚注','强制换行'])
# Commands copied into the actual context menu remain callable.
reset();idx=labels.index('表格')
for i in range(app.context_menu.index('end')+1):
    if app.context_menu.type(i)=='command' and app.context_menu.entrycget(i,'label')=='表格':app.context_menu.invoke(i);break
assert '| 列一 |' in app.content();assert '<table>' in app.html()
reset('- [x] 已完成','1.end')
class Key:widget=app.source
assert app.enter(Key())=='break';assert app.content().endswith('\n- [ ] ')
assert app.enter(Key())=='break';assert app.content().endswith('\n\n')
root.geometry('1040x620');root.update()
bar=app.heading_menu.master.master
for widget in bar.winfo_children():
    assert widget.winfo_x()+widget.winfo_width()<=bar.winfo_width(),(str(widget),widget.winfo_x(),widget.winfo_width(),bar.winfo_width())
root.destroy();print('PASS: toolbar/context commands, selection and caret, H1-H6, lists/tasks, strike rendering, footnotes, code escaping, references, grouped undo')

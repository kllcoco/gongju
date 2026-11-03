import os,sys,tempfile,json,inspect
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));os.environ['MOXIE_DATA']=tempfile.mkdtemp(prefix='guide-check-')
import tkinter as tk
from desktop_app import App
from help_content import TOPICS,manual_markdown
import mcp_server
from syntax_tools import SyntaxTools
root=tk.Tk()
with patch.object(App,'start_bridge',lambda self:None):app=App(root)
app.settext('# 原文\n\n不能被帮助窗口修改。');before=app.content();guide=app.show_about();root.update()
assert len(TOPICS)>=60,len(TOPICS);assert app.content()==before;assert app.show_about() is guide
for topic in TOPICS:
    assert topic['steps'] and topic['template'];guide.show(topic);root.update_idletasks()
    assert guide.template.get('1.0','end-1c')==topic['template']
    if topic['language']=='json':
        data=json.loads(topic['template'])
        if 'name' in data:
            inspect.signature(getattr(mcp_server,data['name'])).bind(**data['arguments'])
    elif topic['language']=='markdown':
        import markdown
        from writing_extension import WritingExtension
        assert markdown.markdown(topic['template'],extensions=['extra','sane_lists','nl2br',WritingExtension()])
    with patch.object(guide.window,'clipboard_clear'),patch.object(guide.window,'clipboard_append') as copy:
        guide.copy_template();copy.assert_called_once_with(topic['template'])
guide.query.set('脚注');guide.filter_topics();root.update();assert guide.tree.get_children();assert app.content()==before
guide.query.set('不存在的功能abcxyz');guide.filter_topics();root.update();assert guide.copy_button.instate(['disabled'])
guide.query.set('');guide.filter_topics();sample=next(t for t in TOPICS if t['title']=='学习笔记模板');guide.show(sample)
with patch.object(app,'confirm',return_value=False):guide.new_document();assert app.content()==before
with patch.object(app,'confirm',return_value=True):guide.new_document();assert app.content()==sample['template'];assert app.dirty();assert app.path is None
newguide=app.show_about();assert newguide.window.winfo_exists();root.update();root.destroy()
manual=manual_markdown(portable=True);assert 'C:\\\\Users' not in manual and 'C:/Users' not in manual
print('PASS:',len(TOPICS),'topics, all templates copy exactly, valid JSON/tool parameters, Markdown parse, search/no results, singleton help, protected current document, new-template dirty state')

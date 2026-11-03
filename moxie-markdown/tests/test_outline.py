import os,sys,tempfile
from pathlib import Path
from unittest.mock import patch
base=Path(__file__).resolve().parents[1];sys.path.insert(0,str(base));os.environ['MOXIE_DATA']=tempfile.mkdtemp(prefix='outline-')
import tkinter as tk
from desktop_app import App
root=tk.Tk()
with patch.object(App,'start_bridge',lambda self:None):app=App(root)
text='# 开头\n\n'+('正文内容\n\n'*45)+'## 重复标题\n\n'+('中间段落\n\n'*45)+'```python\n# 不应出现在大纲中\n```\n\n## 重复标题\n\n'+('后面的段落\n\n'*45)+'下划线标题\n----------\n\n'+('结尾正文\n\n'*45)
app.settext(text);root.update();items=app.tree.get_children();assert len(items)==4,(items,app.renderer.headings)
for key in items:
    app.tree.selection_set(key);root.update()
    mark=app.outline_targets[key]
    assert app.source.index('insert')==key+'.0'
    assert app.source.dlineinfo(key+'.0') is not None
    assert app.preview.dlineinfo(mark) is not None
    assert app.preview.dlineinfo(mark)[1]<50,app.preview.dlineinfo(mark)
assert app.outline_targets[items[1]]!=app.outline_targets[items[2]]
app.mode('read');root.update();app.tree.selection_set(items[1]);root.update();assert app.preview.dlineinfo(app.outline_targets[items[1]]) is not None
app.preview.yview_moveto(0);root.update();box=app.tree.bbox(items[1]);app.tree.event_generate('<ButtonRelease-1>',x=box[0]+20,y=box[1]+box[3]//2);root.update();assert app.preview.dlineinfo(app.outline_targets[items[1]]) is not None
app.mode('both');root.update();app.tree.focus(items[2]);app.tree.selection_set(items[2]);app.tree.event_generate('<Return>');root.update();assert app.source.index('insert')==items[2]+'.0'
app.preview.yview_moveto(.8);before=app.preview.yview()[0];app.render();root.update();assert abs(app.preview.yview()[0]-before)<.02
root.destroy();print('PASS: long document, both panes, read-only mode, repeated titles, code fences, setext headings, repeat clicks, keyboard selection, no scroll reset on render')

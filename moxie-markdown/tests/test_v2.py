import os,sys,tempfile,json,threading,time,asyncio
from pathlib import Path
base=Path(__file__).resolve().parents[1];sys.path.insert(0,str(base))
work=Path(tempfile.mkdtemp(prefix='validation-v2-')).resolve();os.environ['MOXIE_DATA']=str(work/'data')
from core import sha,decode,bounded
from desktop_app import App
from rendering import export_pdf
import tkinter as tk
from unittest.mock import patch
from PIL import Image
root=tk.Tk();root.withdraw();app=App(root)
image=work/'image.png';Image.new('RGB',(120,80),'#137c70').save(image)
text='# 中文测试\n\n**加粗** 和 `code`\n\n| 标题 | 结果 |\n| --- | --- |\n| 中文 | 正常 |\n\n![示例]('+image.as_posix()+')\n'
file=work/'sample.md';file.write_text(text,encoding='utf-8');app.load(file);root.update();assert app.renderer.images and app.renderer.widgets;assert app.tree.get_children()
app.source.insert('end','\n草稿修改');app.checkpoint();assert app.draft.exists()
version=sha(app.content().encode());data={'root':str(work),'action':'replace','text':text+'\nLLM 修改','expected_version':version};result=app.handle(data);assert result['dirty'];assert 'LLM 修改' in app.content()
try:app.handle(data);raise AssertionError('stale edit accepted')
except ValueError:pass
try:app.handle({'root':str(work/'elsewhere'),'action':'read'});raise AssertionError('scope escape accepted')
except ValueError:pass
result=app.handle({'root':str(work),'action':'save','expected_version':result['version']});assert not result['dirty'];assert not app.draft.exists();assert list((work/'data/history').glob('*/*.bak'))
export_pdf(app.html(),work/'sample.pdf',work);assert (work/'sample.pdf').read_bytes().startswith(b'%PDF')
app.source.mark_set('insert','1.5');before=app.content();app.insert('## ','',True);assert '中文测试' in app.content();app.source.edit_undo();root.update();assert app.content()==before
app.source.tag_add('sel','1.0','1.3');before=app.content();app.insert('**','**');app.source.edit_undo();assert app.content()==before
app.source.insert('end','\n崩溃恢复验证');app.checkpoint()
old=app.draft.with_name('previous-crash.json');old.write_bytes(app.draft.read_bytes());assert old in app.restore_candidates()
app.saved=app.content();app.close();print('PASS native: images, table widgets, PDF, recovery, history, editor version checks, scope, undo')

async def protocol():
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    params=StdioServerParameters(command=sys.executable,args=[str(base/'mcp_server.py'),'--root',str(work/'workspace')],env={**os.environ,'MOXIE_DATA':str(work/'data')})
    async with Client(params) as client:
        tools=await client.list_tools();assert len(tools.tools)==11
        async def call(name,args):
            result=await client.call_tool(name,args)
            if result.is_error:raise AssertionError(str(result))
            return result.structured_content or json.loads(result.content[0].text)
        d=await call('create_document',{'path':'notes/test.md','text':'# MCP 测试\nhello'})
        assert d['version']
        r=await call('read_document',{'path':'notes/test.md'});assert r['text']=='# MCP 测试\nhello'
        edited=await call('replace_text',{'path':'notes/test.md','old_text':'hello','new_text':'world','expected_version':r['version']});assert 'world' in edited['text']
        bad=await client.call_tool('write_document',{'path':'notes/test.md','text':'oops','expected_version':r['version']});assert bad.is_error
        bad=await client.call_tool('read_document',{'path':'../../sample.md'});assert bad.is_error
        bad=await client.call_tool('create_document',{'path':'notes/test.md','text':'oops'});assert bad.is_error
        print('PASS MCP stdio: initialization, 11 tools, create/read/replace, stale-write rejection, traversal rejection, no clobber')
asyncio.run(protocol())


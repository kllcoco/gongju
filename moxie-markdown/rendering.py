from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote,urlparse
import tkinter as tk
from PIL import Image,ImageTk
class Node:
    def __init__(self,tag='root',attrs=None):self.tag=tag;self.attrs=dict(attrs or []);self.children=[]
    def text(self):return ''.join(c if isinstance(c,str) else c.text() for c in self.children)
class Tree(HTMLParser):
    def __init__(self,html):
        super().__init__();self.root=Node();self.stack=[self.root];self.feed(html)
    def handle_starttag(self,tag,attrs):
        n=Node(tag,attrs);self.stack[-1].children.append(n)
        if tag not in ('img','hr','br','input','meta','link'):self.stack.append(n)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:self.stack=self.stack[:i];break
    def handle_data(self,data):self.stack[-1].children.append(data)
def local_image(src,base):
    src=unquote(src)
    if urlparse(src).scheme not in ('', 'file') and not (len(src)>2 and src[1]==':'):return None
    p=Path(src.removeprefix('file:///'));p=p if p.is_absolute() else base/p
    if p.is_file() and p.stat().st_size<20*1024*1024:return p
    return None
class Renderer:
    def __init__(self,w,base):self.w=w;self.base=base;self.images=[];self.widgets=[];self.links=0;self.headings=[]
    def render(self,html):
        for child in self.w.winfo_children():child.destroy()
        for mark in self.w.mark_names():
            if mark.startswith("outline_heading_"):self.w.mark_unset(mark)
        self.walk(Tree(html).root)
    def walk(self,node,tags=()):
        if isinstance(node,str):self.w.insert('end',node,tags);return
        tag=node.tag
        if tag in ('script','style','iframe','object'):return
        if tag=='input':
            if node.attrs.get('type')=='checkbox':self.w.insert('end','☑ ' if 'checked' in node.attrs else '☐ ')
            return
        if tag=='table':
            rows=[]
            def gather(n):
                if isinstance(n,str):return
                if n.tag=='tr':rows.append([c for c in n.children if not isinstance(c,str) and c.tag in ('td','th')])
                else:
                    for c in n.children:gather(c)
            gather(node);frame=tk.Frame(self.w,bg='#cbd7e2');self.widgets.append(frame)
            for i,row in enumerate(rows):
                for j,cell in enumerate(row):
                    label=tk.Label(frame,text=cell.text().strip(),font=('Microsoft YaHei UI',11,'bold' if cell.tag=='th' else 'normal'),bg='#eaf3f0' if cell.tag=='th' else '#fff',fg='#263445',padx=10,pady=8,wraplength=max(90,320//max(1,len(row))),justify='left',anchor='w')
                    label.grid(row=i,column=j,sticky='nsew',padx=1,pady=1);frame.columnconfigure(j,weight=1)
            self.w.insert('end','\n');self.w.window_create('end',window=frame);self.w.insert('end','\n\n');return
        if tag=='img':
            path=local_image(node.attrs.get('src',''),self.base)
            try:
                if not path:raise ValueError()
                with Image.open(path) as im:
                    im.thumbnail((max(300,self.w.winfo_width()-60),500));photo=ImageTk.PhotoImage(im.copy(),master=self.w)
                self.images.append(photo);self.w.insert('end','\n');self.w.image_create('end',image=photo);self.w.insert('end','\n')
            except Exception:self.w.insert('end','[图片：'+node.attrs.get('alt','无法加载；网络图片需先下载到本地')+']','muted')
            return
        if tag in ('p','pre','blockquote') or tag.startswith('h') and tag[1:].isdigit():self.w.insert('end','\n')
        if tag in ('h1','h2','h3','h4','h5','h6'):
            mark='outline_heading_'+str(len(self.headings));self.w.mark_set(mark,'end-1c');self.w.mark_gravity(mark,'left')
            self.headings.append({'level':int(tag[1]),'text':node.text().strip(),'mark':mark})
        style={'strong':'bold','b':'bold','em':'italic','code':'code','pre':'code','blockquote':'quote'}.get(tag,tag)
        if tag=='a':
            import webbrowser
            style=f'link{self.links}';self.links+=1;url=node.attrs.get('href','');self.w.tag_configure(style,foreground='#087c72',underline=True)
            self.w.tag_bind(style,'<Button-1>',lambda e,u=url:webbrowser.open(u) if u.startswith(('https://','http://')) else None)
        if tag=='li' and not any(not isinstance(c,str) and (c.tag=='input' or c.tag=='p' and any(not isinstance(x,str) and x.tag=='input' for x in c.children)) for c in node.children):self.w.insert('end','  •  ')
        if tag=='br':self.w.insert('end','\n')
        if tag=='hr':self.w.insert('end','\n'+'─'*35+'\n','muted')
        for child in node.children:self.walk(child,tags+(style,))
        if tag in ('p','li','pre','blockquote') or tag.startswith('h') and tag[1:].isdigit():self.w.insert('end','\n')

def export_pdf(html,path,base):
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image as PDFImage
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib import colors
    from html import escape
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    normal=ParagraphStyle('body',fontName='STSong-Light',fontSize=11,leading=18,wordWrap='CJK',spaceAfter=8)
    story=[]
    def para(text,size=11):
        st=ParagraphStyle('p',parent=normal,fontSize=size,leading=size*1.6)
        return Paragraph(escape(text).replace('\n','<br/>'),st)
    def walk(n):
        if isinstance(n,str):
            if n.strip():story.append(para(n))
            return
        if n.tag in ('script','style','iframe'):return
        if n.tag=='table':
            rows=[]
            def collect(x):
                if isinstance(x,str):return
                if x.tag=='tr':rows.append([para(c.text()) for c in x.children if not isinstance(c,str) and c.tag in ('td','th')])
                else:
                    for c in x.children:collect(c)
            collect(n)
            if rows:
                table=Table(rows,colWidths=[465/max(map(len,rows))]*max(map(len,rows)),repeatRows=1,hAlign='LEFT');table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.5,colors.HexColor('#cbd7e2')),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eaf3f0')),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]));story.extend([table,Spacer(1,12)])
        elif n.tag=='img':
            p=local_image(n.attrs.get('src',''),base)
            if p:
                with Image.open(p) as im:w,h=im.size
                scale=min(465/w,500/h,1);story.append(PDFImage(str(p),width=w*scale,height=h*scale))
            else:story.append(para('[图片] '+n.attrs.get('alt','')))
        elif n.tag in ('p','pre','li','blockquote','h1','h2','h3','h4','h5','h6') and not any(not isinstance(c,str) and c.tag=='img' for c in n.children):story.append(para(('• ' if n.tag=='li' else '')+n.text(),{'h1':22,'h2':18,'h3':15}.get(n.tag,11)))
        else:
            for c in n.children:walk(c)
    walk(Tree(html).root)
    SimpleDocTemplate(str(path),rightMargin=60,leftMargin=60,topMargin=50,bottomMargin=50,title=Path(path).stem).build(story or [para(' ')])

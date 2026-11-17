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
LIGHT={'table_head':'#e7f3f0','table_alt':'#f8fafb','table_line':'#d9e0e6','surface':'#ffffff','text':'#1f2933','link':'#0b7d70','muted':'#7b8794'}
class Renderer:
    def __init__(self,w,base,palette=None,on_link=None,font='Microsoft YaHei UI'):
        self.w=w;self.base=base;self.images=[];self.widgets=[];self.links=0;self.headings=[];self.p=palette or LIGHT;self.on_link=on_link;self.font=font;self.lists=[]
    def render(self,html):
        for child in self.w.winfo_children():child.destroy()
        for mark in self.w.mark_names():
            if mark.startswith("outline_heading_"):self.w.mark_unset(mark)
        for tag in self.w.tag_names():
            if tag.startswith('link'):self.w.tag_delete(tag)
        self.walk(Tree(html).root)
    def open_link(self,url):
        if self.on_link:self.on_link(url)
        elif url.startswith(('https://','http://')):
            import webbrowser;webbrowser.open(url)
    def block_break(self):
        # Start blocks on a fresh line without stacking blank lines.
        if 'marker' in self.w.tag_names('end-2c'):return
        if self.w.get('end-2c','end-1c') not in ('\n',''):self.w.insert('end','\n')
    def table(self,node):
        rows=[]
        def gather(n):
            if isinstance(n,str):return
            if n.tag=='tr':rows.append([c for c in n.children if not isinstance(c,str) and c.tag in ('td','th')])
            else:
                for c in n.children:gather(c)
        gather(node);p=self.p;frame=tk.Frame(self.w,bg=p['table_line']);self.widgets.append(frame)
        width=self.w.winfo_width();width=max(360,(width if width>100 else 760)-110);cols=max([len(r) for r in rows]+[1])
        for i,row in enumerate(rows):
            for j,cell in enumerate(row):
                head=cell.tag=='th';bg=p['table_head'] if head else p['table_alt'] if i%2==0 else p['surface']
                align=(cell.attrs.get('style') or '').replace(' ','')
                anchor='e' if 'text-align:right' in align else 'center' if 'text-align:center' in align else 'w'
                label=tk.Label(frame,text=cell.text().strip(),font=(self.font,10,'bold' if head else 'normal'),bg=bg,fg=p['text'],padx=12,pady=7,wraplength=max(100,width//cols),justify='left',anchor=anchor)
                label.grid(row=i,column=j,sticky='nsew',padx=(0 if j else 1,1),pady=(0 if i else 1,1));frame.columnconfigure(j,weight=1)
        self.block_break();self.w.window_create('end',window=frame,padx=8);self.w.insert('end','\n')
    def walk(self,node,tags=()):
        if isinstance(node,str):
            if 'pre' not in tags and not node.strip() and '\n' in node:return
            self.w.insert('end',node,tags);return
        tag=node.tag
        if tag in ('script','style','iframe','object'):return
        if tag=='input':
            if node.attrs.get('type')=='checkbox':self.w.insert('end','☑ ' if 'checked' in node.attrs else '☐ ',tags+('marker',))
            return
        if tag=='table':self.table(node);return
        if tag=='img':
            path=local_image(node.attrs.get('src',''),self.base)
            try:
                if not path:raise ValueError()
                with Image.open(path) as im:
                    im.thumbnail((max(300,self.w.winfo_width()-90),520));photo=ImageTk.PhotoImage(im.copy(),master=self.w)
                self.images.append(photo);self.block_break();self.w.image_create('end',image=photo,padx=8,pady=6);self.w.insert('end','\n')
            except Exception:self.w.insert('end','[图片：'+node.attrs.get('alt','无法加载；网络图片需先下载到本地')+']','muted')
            return
        heading=tag in ('h1','h2','h3','h4','h5','h6')
        if tag in ('p','pre','blockquote','ul','ol','div') or heading:self.block_break()
        if heading:
            mark='outline_heading_'+str(len(self.headings));self.w.mark_set(mark,'end-1c');self.w.mark_gravity(mark,'left')
            self.headings.append({'level':int(tag[1]),'text':node.text().strip(),'mark':mark})
        style={'strong':'bold','b':'bold','em':'italic','i':'italic','code':'code','pre':'pre','blockquote':'quote','del':'del','s':'del','sup':'muted'}.get(tag,tag)
        if tag=='a':
            style=f'link{self.links}';self.links+=1;url=node.attrs.get('href','');self.w.tag_configure(style,foreground=self.p['link'],underline=True)
            self.w.tag_bind(style,'<Button-1>',lambda e,u=url:self.open_link(u));self.w.tag_bind(style,'<Enter>',lambda e:self.w.config(cursor='hand2'));self.w.tag_bind(style,'<Leave>',lambda e:self.w.config(cursor=''))
        if tag in ('ul','ol'):
            try:start=int(node.attrs.get('start','1'))
            except ValueError:start=1
            self.lists.append([tag,start])
            for child in node.children:self.walk(child,tags)
            self.lists.pop();self.block_break();return
        if tag=='li':
            depth=min(6,max(1,len(self.lists)));kind,number=self.lists[-1] if self.lists else ('ul',1)
            style='indent'+str(depth)
            has_box=any(not isinstance(c,str) and (c.tag=='input' or c.tag=='p' and any(not isinstance(x,str) and x.tag=='input' for x in c.children)) for c in node.children)
            self.block_break()
            if not has_box:
                marker=f'{number}.  ' if kind=='ol' else ('•','◦','▪')[(depth-1)%3]+'  '
                self.w.insert('end',marker,tags+(style,'marker'))
            if self.lists:self.lists[-1][1]+=1
        if tag=='br':self.w.insert('end','\n')
        if tag=='pre':self.w.insert('end','\n',tags+('pre','prepad'))
        if tag=='blockquote' and 'quote' not in tags and self.w.get('1.0','end-1c'):self.w.insert('end','\n','gap')
        if tag=='hr':self.block_break();self.w.insert('end','─'*40+'\n','muted')
        for child in node.children:self.walk(child,tags+(style,) if tag not in ('ul','ol') else tags)
        if tag in ('p','li','pre','blockquote') or heading:
            if tag=='blockquote':
                if 'quote' not in tags:self.w.insert('end','\n','gap')
            elif tag=='pre':
                if self.w.get('end-2c','end-1c')!='\n':self.w.insert('end','\n',tags+(style,))
                self.w.insert('end','\n',tags+(style,'prepad'));self.w.insert('end','\n','gap')
            elif self.w.get('end-2c','end-1c')!='\n':self.w.insert('end','\n',tags+(style,) if tag in ('li','blockquote') else tags)

def _pdf_fonts():
    """Prefer real Windows/Linux CJK TrueType fonts (with bold); fall back to the built-in CID font."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.fonts import addMapping
    import os
    windir=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    pairs=[(windir/'msyh.ttc',windir/'msyhbd.ttc'),(windir/'simhei.ttf',None),
           (Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'))]
    for regular,bold in pairs:
        if regular.is_file():
            try:
                pdfmetrics.registerFont(TTFont('MoxieSans',str(regular),subfontIndex=0))
                if bold and bold.is_file():pdfmetrics.registerFont(TTFont('MoxieSans-Bold',str(bold),subfontIndex=0))
                else:pdfmetrics.registerFont(TTFont('MoxieSans-Bold',str(regular),subfontIndex=0))
                for b in (0,1):
                    for it in (0,1):addMapping('MoxieSans',b,it,'MoxieSans-Bold' if b else 'MoxieSans')
                return 'MoxieSans',True
            except Exception:continue
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    for b in (0,1):
        for it in (0,1):addMapping('STSong-Light',b,it,'STSong-Light')
    return 'STSong-Light',False

def export_pdf(html,path,base):
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image as PDFImage,HRFlowable
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from html import escape
    import re
    family,_=_pdf_fonts()
    accent=colors.HexColor('#0f8b7d');muted=colors.HexColor('#6b7785');codebg=colors.HexColor('#f3f5f7')
    body=ParagraphStyle('body',fontName=family,fontSize=10.5,leading=17,wordWrap='CJK',spaceAfter=7,textColor=colors.HexColor('#1f2933'))
    sizes={'h1':20,'h2':16,'h3':13.5,'h4':12,'h5':11,'h6':10.5}
    heads={t:ParagraphStyle(t,parent=body,fontName=family+('-Bold' if family=='MoxieSans' else ''),fontSize=z,leading=z*1.45,spaceBefore=10 if t in ('h1','h2') else 6,spaceAfter=6,textColor=muted if t=='h6' else body.textColor) for t,z in sizes.items()}
    code=ParagraphStyle('code',parent=body,fontSize=9.5,leading=14,backColor=codebg,borderPadding=(7,8,7,8),leftIndent=8,rightIndent=8,spaceBefore=4,spaceAfter=10,wordWrap='CJK')
    quote=ParagraphStyle('quote',parent=body,leftIndent=14,textColor=muted,borderColor=colors.HexColor('#9fd8cc'),borderWidth=0,borderPadding=(2,0,2,6))
    story=[]
    def mono(text,pre=False):
        # Courier lacks Chinese glyphs: keep ASCII runs in Courier and everything else in the CJK font.
        out=[]
        for run in re.findall(r'[\x20-\x7e]+|[^\x20-\x7e]+',text):
            piece=escape(run)
            if pre:piece=piece.replace('\t','    ').replace(' ','&nbsp;').replace('\n','<br/>')
            out.append(f'<font face="Courier">{piece}</font>' if run[0].isascii() and run[0]!='\n' else piece)
        return ''.join(out)
    def inline(n):
        if isinstance(n,str):return escape(n).replace('\n',' ')
        t=n.tag;inner=lambda:''.join(inline(c) for c in n.children)
        if t in ('strong','b'):return '<b>'+inner()+'</b>'
        if t in ('em','i'):return '<i>'+inner()+'</i>'
        if t in ('del','s'):return '<strike>'+inner()+'</strike>'
        if t=='code':return '<font backColor="#eef1f4" color="#8a3f9e">'+mono(n.text())+'</font>'
        if t=='a':
            href=n.attrs.get('href','')
            return f'<a href="{escape(href)}" color="#0b7d70"><u>{inner()}</u></a>' if href.startswith(('http://','https://','mailto:')) else '<font color="#0b7d70">'+inner()+'</font>'
        if t=='br':return '<br/>'
        if t=='sup':return '<super>'+inner()+'</super>'
        if t=='input':return '[√] ' if 'checked' in n.attrs else '[  ] '
        if t=='img':return '[图片：'+escape(n.attrs.get('alt',''))+']'
        return inner()
    def image(n):
        p=local_image(n.attrs.get('src',''),base)
        if not p:story.append(Paragraph('[图片] '+escape(n.attrs.get('alt','')),body));return
        with Image.open(p) as im:w,h=im.size
        scale=min(470/w,520/h,1);story.append(PDFImage(str(p),width=w*scale,height=h*scale));story.append(Spacer(1,8))
    def paragraph(n,style):
        parts=[];buf=[]
        for c in n.children:
            if not isinstance(c,str) and c.tag=='img':
                if ''.join(buf).strip():story.append(Paragraph(''.join(buf),style))
                buf=[];image(c)
            else:buf.append(inline(c))
        if ''.join(buf).strip():story.append(Paragraph(''.join(buf),style))
    def lst(n,depth):
        ordered=n.tag=='ol'
        try:number=int(n.attrs.get('start','1'))
        except ValueError:number=1
        for li in n.children:
            if isinstance(li,str) or li.tag!='li':continue
            st=ParagraphStyle('li',parent=body,leftIndent=16*depth+6,bulletIndent=16*(depth-1)+4,spaceAfter=3)
            text=[];nested=[]
            for c in li.children:
                if not isinstance(c,str) and c.tag in ('ul','ol'):nested.append(c)
                elif not isinstance(c,str) and c.tag=='p':text.append(''.join(inline(x) for x in c.children)+' ')
                else:text.append(inline(c))
            has_box=any(not isinstance(c,str) and c.tag=='input' for c in li.children) or any(not isinstance(c,str) and c.tag=='p' and any(not isinstance(x,str) and x.tag=='input' for x in c.children) for c in li.children)
            bullet='' if has_box else (f'{number}.' if ordered else ('•','◦','▪')[(depth-1)%3])
            story.append(Paragraph(''.join(text).strip() or ' ',st,bulletText=bullet or None))
            number+=1
            for sub in nested:lst(sub,depth+1)
    def table(n):
        rows=[];heads_row=0
        def collect(x):
            if isinstance(x,str):return
            if x.tag=='tr':rows.append([Paragraph(''.join(inline(c) for c in cell.children),body) for cell in x.children if not isinstance(cell,str) and cell.tag in ('td','th')])
            else:
                for c in x.children:collect(c)
        collect(n)
        if not rows:return
        cols=max(map(len,rows));rows=[r+[Paragraph('',body)]*(cols-len(r)) for r in rows]
        t=Table(rows,colWidths=[470/cols]*cols,repeatRows=1,hAlign='LEFT')
        style=[('GRID',(0,0),(-1,-1),.5,colors.HexColor('#d9e0e6')),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e7f3f0')),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),4)]
        style+=[('BACKGROUND',(0,i),(-1,i),colors.HexColor('#f8fafb')) for i in range(2,len(rows),2)]
        t.setStyle(TableStyle(style));story.extend([t,Spacer(1,10)])
    def walk(n,inquote=False):
        if isinstance(n,str):
            if n.strip():story.append(Paragraph(escape(n),body))
            return
        t=n.tag
        if t in ('script','style','iframe'):return
        if t in heads:paragraph(n,heads[t])
        elif t=='p':paragraph(n,quote if inquote else body)
        elif t=='pre':story.append(Paragraph(mono(n.text().rstrip('\n'),True),code))
        elif t in ('ul','ol'):lst(n,1)
        elif t=='table':table(n)
        elif t=='hr':story.append(HRFlowable(width='100%',thickness=.6,color=colors.HexColor('#d9e0e6'),spaceBefore=6,spaceAfter=10))
        elif t=='img':image(n)
        elif t=='blockquote':
            for c in n.children:walk(c,True)
        else:
            for c in n.children:walk(c,inquote)
    walk(Tree(html).root)
    SimpleDocTemplate(str(path),rightMargin=60,leftMargin=60,topMargin=50,bottomMargin=50,title=Path(path).stem).build(story or [Paragraph(' ',body)])

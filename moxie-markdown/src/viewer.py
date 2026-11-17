"""Read-only in-app window for bundled Markdown documents (instead of handing them to other programs)."""
import tkinter as tk
from tkinter import ttk
from pathlib import Path
import markdown
import theme
from core import decode
from rendering import Renderer
from writing_extension import WritingExtension

class DocViewer:
    def __init__(self, app, path, title=None):
        self.app = app; self.path = Path(path); p = app.p
        self.window = win = tk.Toplevel(app.root)
        win.title((title or self.path.stem) + ' · 墨写'); win.geometry('900x760'); win.minsize(600, 420); win.transient(app.root); win.configure(bg=p['bg'])
        bar = ttk.Frame(win, padding=(18, 10)); bar.pack(fill='x')
        ttk.Label(bar, text=title or self.path.stem, style='File.TLabel').pack(side='left')
        ttk.Button(bar, text='关闭', command=win.destroy).pack(side='right')
        ttk.Button(bar, text='在编辑区打开', command=self.edit).pack(side='right', padx=6)
        body = ttk.Frame(win, style='Surface.TFrame'); body.pack(fill='both', expand=True)
        self.text = tk.Text(body, wrap='word'); scroll = ttk.Scrollbar(body, command=self.text.yview)
        scroll.pack(side='right', fill='y'); self.text.configure(yscrollcommand=scroll.set); self.text.pack(fill='both', expand=True)
        # Reuse the main preview's tag styles so the document looks identical to the reading pane.
        self.text.configure(**theme.text_options(p, app.fonts['ui'], app.size + 1, False)); self.text.configure(padx=40)
        for tag in app.preview.tag_names():
            if tag.startswith(('h', 'indent')) or tag in ('bold', 'italic', 'del', 'code', 'pre', 'quote', 'muted', 'prepad', 'p', 'li', 'marker'):
                opts = {k: v[-1] for k, v in ((k, app.preview.tag_configure(tag, k)) for k in ('font', 'foreground', 'background', 'lmargin1', 'lmargin2', 'rmargin', 'spacing1', 'spacing3', 'overstrike', 'lmargincolor')) if v and v[-1] not in ('', None)}
                try: self.text.tag_configure(tag, **opts)
                except tk.TclError: pass
        win.bind('<Escape>', lambda e: win.destroy())
        self.render()
    def render(self):
        raw = decode(self.path.read_bytes())
        html = markdown.markdown(raw, extensions=['extra', 'sane_lists', 'nl2br', WritingExtension()])
        self.text.config(state='normal'); self.text.delete('1.0', 'end')
        self.renderer = Renderer(self.text, self.path.parent, self.app.p, lambda u: self.app.open_link(u, self.path.parent), self.app.fonts['ui']); self.renderer.render(html)
        self.text.config(state='disabled')
    def edit(self):
        if self.app.confirm():
            self.window.destroy(); self.app.load(self.path)

"""Light/dark palettes, font selection, ttk styling and drawn toolbar icons."""
import tkinter as tk
from tkinter import ttk, font as tkfont
from PIL import Image, ImageDraw, ImageTk

PALETTES = {
    'light': {
        'bg': '#f6f7f9', 'panel': '#eef1f4', 'surface': '#ffffff', 'text': '#1f2933', 'muted': '#7b8794',
        'faint': '#a3adb8', 'accent': '#0f8b7d', 'accent_soft': '#dcf1ec', 'border': '#e1e5ea', 'hover': '#e4e8ec',
        'code_bg': '#f3f5f7', 'code_fg': '#8a3f9e', 'quote_bar': '#9fd8cc', 'quote_bg': '#f6fbfa', 'sel': '#c9ebe3',
        'table_head': '#e7f3f0', 'table_alt': '#f8fafb', 'table_line': '#d9e0e6', 'curline': '#f5f8fa', 'link': '#0b7d70',
    },
    'dark': {
        'bg': '#1b1e23', 'panel': '#202329', 'surface': '#23272e', 'text': '#d8dde3', 'muted': '#8b95a1',
        'faint': '#5d6672', 'accent': '#4fd1b9', 'accent_soft': '#23413c', 'border': '#30353d', 'hover': '#2c3139',
        'code_bg': '#2b3038', 'code_fg': '#d7a6e8', 'quote_bar': '#3f8f80', 'quote_bg': '#252b30', 'sel': '#2f5d55',
        'table_head': '#29363a', 'table_alt': '#262a31', 'table_line': '#3a4049', 'curline': '#272b33', 'link': '#5fd6c1',
    },
}

def pick(root, candidates, fallback):
    families = {f.lower(): f for f in tkfont.families(root)}
    for name in candidates:
        if name.lower() in families: return families[name.lower()]
    return fallback

def fonts(root):
    ui = pick(root, ['Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', 'Noto Sans CJK TC', 'Noto Sans SC', 'Source Han Sans SC', 'WenQuanYi Micro Hei'], 'TkDefaultFont')
    # Mono fonts that contain Chinese glyphs come first; otherwise Chinese falls back to blurry system fonts.
    mono_cjk = pick(root, ['Sarasa Mono SC', 'Sarasa Term SC', 'Maple Mono NF CN', 'Noto Sans Mono CJK SC'], None)
    mono = mono_cjk or pick(root, ['Cascadia Mono', 'JetBrains Mono', 'Consolas', 'DejaVu Sans Mono'], 'TkFixedFont')
    return {'ui': ui, 'mono': mono, 'mono_has_cjk': bool(mono_cjk)}

def style_ttk(root, p, f):
    s = ttk.Style(root)
    s.theme_use('clam')
    base = (f['ui'], 10)
    s.configure('.', background=p['bg'], foreground=p['text'], font=base, bordercolor=p['border'], lightcolor=p['bg'], darkcolor=p['bg'], troughcolor=p['panel'], focuscolor=p['accent'], selectbackground=p['sel'], selectforeground=p['text'], fieldbackground=p['surface'], insertcolor=p['text'])
    s.configure('TFrame', background=p['bg'])
    s.configure('Panel.TFrame', background=p['panel'])
    s.configure('Surface.TFrame', background=p['surface'])
    s.configure('TLabel', background=p['bg'], foreground=p['text'], font=base)
    s.configure('Panel.TLabel', background=p['panel'])
    s.configure('Muted.TLabel', background=p['bg'], foreground=p['muted'], font=(f['ui'], 9))
    s.configure('Head.TLabel', background=p['surface'], foreground=p['faint'], font=(f['ui'], 9))
    s.configure('PanelHead.TLabel', background=p['panel'], foreground=p['faint'], font=(f['ui'], 9, 'bold'))
    s.configure('Brand.TLabel', background=p['bg'], foreground=p['accent'], font=(f['ui'], 15, 'bold'))
    s.configure('File.TLabel', background=p['bg'], foreground=p['text'], font=(f['ui'], 11))
    s.configure('Dirty.TLabel', background=p['bg'], foreground=p['accent'], font=(f['ui'], 11))
    s.configure('Status.TLabel', background=p['panel'], foreground=p['muted'], font=(f['ui'], 9))
    s.configure('Status.TFrame', background=p['panel'])
    s.configure('TButton', background=p['surface'], foreground=p['text'], padding=(12, 6), relief='flat', borderwidth=1, bordercolor=p['border'], font=base)
    s.map('TButton', background=[('pressed', p['accent_soft']), ('active', p['hover'])], bordercolor=[('focus', p['accent'])])
    s.configure('Tool.TButton', background=p['bg'], borderwidth=0, padding=(9, 6), relief='flat', font=(f['ui'], 10), width=0)
    s.map('Tool.TButton', background=[('pressed', p['accent_soft']), ('active', p['hover'])])
    s.configure('Accent.TButton', background=p['accent'], foreground='#ffffff', bordercolor=p['accent'])
    s.map('Accent.TButton', background=[('active', p['accent']), ('pressed', p['accent'])])
    s.configure('TEntry', fieldbackground=p['surface'], foreground=p['text'], bordercolor=p['border'], lightcolor=p['border'], padding=6)
    s.map('TEntry', bordercolor=[('focus', p['accent'])], lightcolor=[('focus', p['accent'])])
    s.configure('Treeview', background=p['panel'], fieldbackground=p['panel'], foreground=p['text'], rowheight=30, borderwidth=0, relief='flat', font=(f['ui'], 10))
    s.map('Treeview', background=[('selected', p['accent_soft'])], foreground=[('selected', p['accent'])])
    s.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
    s.configure('TPanedwindow', background=p['border'])
    s.configure('Sash', sashthickness=1, background=p['border'], gripcount=0)
    for orient in ('Vertical', 'Horizontal'):
        s.configure(orient + '.TScrollbar', background=p['panel'], troughcolor=p['surface'], bordercolor=p['surface'], arrowcolor=p['faint'], lightcolor=p['panel'], darkcolor=p['panel'], gripcount=0, arrowsize=11)
        s.map(orient + '.TScrollbar', background=[('active', p['hover'])])
    s.configure('TCheckbutton', background=p['bg'], foreground=p['text'])
    s.map('TCheckbutton', background=[('active', p['bg'])])
    root.configure(bg=p['bg'])
    root.option_add('*Menu.background', p['surface'])
    root.option_add('*Menu.foreground', p['text'])
    root.option_add('*Menu.activeBackground', p['accent_soft'])
    root.option_add('*Menu.activeForeground', p['text'])
    root.option_add('*Menu.relief', 'flat')
    root.option_add('*Listbox.background', p['surface'])
    root.option_add('*Listbox.foreground', p['text'])
    root.option_add('*Listbox.selectBackground', p['sel'])
    root.option_add('*Listbox.selectForeground', p['text'])
    root.option_add('*Listbox.relief', 'flat')
    root.option_add('*Listbox.highlightThickness', 0)
    return s

def text_options(p, family, size, editable=True):
    return dict(font=(family, size), bg=p['surface'], fg=p['text'], insertbackground=p['accent'], selectbackground=p['sel'], selectforeground=p['text'], inactiveselectbackground=p['sel'], relief='flat', borderwidth=0, highlightthickness=0, insertwidth=2, padx=30, pady=22, spacing1=3, spacing2=2, spacing3=4)

def recolor_menu(menu, p):
    try:
        menu.configure(bg=p['surface'], fg=p['text'], activebackground=p['accent_soft'], activeforeground=p['text'], relief='flat', borderwidth=1)
        for i in range((menu.index('end') or -1) + 1):
            if menu.type(i) == 'cascade':
                child = menu.nametowidget(menu.entrycget(i, 'menu'))
                recolor_menu(child, p)
    except tk.TclError:
        pass

# Icons are drawn at 4x and downsampled so they stay sharp without bundling image files.
def _canvas():
    im = Image.new('RGBA', (72, 72), (0, 0, 0, 0)); return im, ImageDraw.Draw(im)

def _icon(kind, color):
    im, d = _canvas(); w = 6
    if kind == 'new':
        d.polygon([(16, 8), (44, 8), (58, 22), (58, 64), (16, 64)], outline=color, width=w); d.line([(44, 8), (44, 22), (58, 22)], fill=color, width=w)
        d.line([(37, 32), (37, 54)], fill=color, width=w); d.line([(26, 43), (48, 43)], fill=color, width=w)
    elif kind == 'open':
        d.line([(8, 20), (8, 60), (58, 60), (64, 30), (20, 30), (12, 58)], fill=color, width=w, joint='curve')
        d.line([(8, 20), (26, 20), (32, 26), (54, 26), (54, 30)], fill=color, width=w, joint='curve')
    elif kind == 'save':
        d.rounded_rectangle([10, 10, 62, 62], radius=7, outline=color, width=w); d.rectangle([22, 12, 48, 28], outline=color, width=w); d.rounded_rectangle([20, 40, 52, 60], radius=3, outline=color, width=w)
    elif kind == 'find':
        d.ellipse([10, 10, 46, 46], outline=color, width=w); d.line([(42, 42), (62, 62)], fill=color, width=w + 2)
    elif kind == 'moon':
        d.ellipse([10, 10, 62, 62], fill=color); d.ellipse([26, 2, 74, 50], fill=(0, 0, 0, 0))
    elif kind == 'sun':
        d.ellipse([24, 24, 48, 48], outline=color, width=w)
        for a, b in [((36, 4), (36, 14)), ((36, 58), (36, 68)), ((4, 36), (14, 36)), ((58, 36), (68, 36)), ((13, 13), (20, 20)), ((52, 52), (59, 59)), ((13, 59), (20, 52)), ((52, 20), (59, 13))]:
            d.line([a, b], fill=color, width=w)
    elif kind == 'layout':
        d.rounded_rectangle([8, 12, 64, 60], radius=6, outline=color, width=w); d.line([(36, 12), (36, 60)], fill=color, width=w)
    return im.resize((18, 18), Image.LANCZOS)

def icons(root, p):
    return {k: ImageTk.PhotoImage(_icon(k, p['muted']), master=root) for k in ('new', 'open', 'save', 'find', 'moon', 'sun', 'layout')}

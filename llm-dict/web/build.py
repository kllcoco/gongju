"""把 App 的 www 目录拼成一个可以在电脑浏览器里直接打开的网页。用法: python3 build.py <www目录> <输出.html>"""
import sys, glob, os
w, out = sys.argv[1], sys.argv[2]
rd = lambda p: open(os.path.join(w, p), encoding='utf8').read()
sc = lambda s: '<script>' + s.replace('</script', '<\\/script') + '</script>'
data = sc(rd('data/index.js'))
data += sc("window.__PRE={};window.DICT_LOAD=function(n,r){window.__PRE[n]=r};window.__KPRE={};window.DICT_COURSE=function(n,d){window.__KPRE[n]=d};window.DICT_EXAM=function(d){window.__XPRE=d};")
for f in sorted(glob.glob(os.path.join(w, 'data/c_*.js'))) + sorted(glob.glob(os.path.join(w, 'data/k_*.js'))) + [os.path.join(w, 'data/x.js')]: data += sc(open(f, encoding='utf8').read())
data += sc(rd('app.js'))
html = rd('index.html').replace('<link rel="stylesheet" href="app.css">', '<style>' + rd('app.css') + '</style>').replace('<script src="data/index.js"></script>\n<script src="app.js"></script>', data)
assert 'app.js"' not in html
open(out, 'w', encoding='utf8').write(html); print(len(html.encode()))

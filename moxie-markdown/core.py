"""Shared document storage and bounded MCP workspace access."""
import os,sys,json,hashlib,tempfile,threading,time
from pathlib import Path
BASE=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
DATA=Path(os.environ.get('MOXIE_DATA',str(BASE/'data')))
DATA.mkdir(parents=True,exist_ok=True)
ASSETS=Path(getattr(sys,'_MEIPASS',BASE))/'assets'
MAX_BYTES=20*1024*1024
LOCK=threading.RLock()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def atomic(path,raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);name=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:name=f.name;f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if name and os.path.exists(name):os.unlink(name)
def decode(raw):
    if len(raw)>MAX_BYTES:raise ValueError('Document exceeds 20 MB')
    if b'\x00' in raw[:4096] and not raw.startswith((b'\xff\xfe',b'\xfe\xff')):raise ValueError('Not a text document')
    for enc in (['utf-16'] if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else ['utf-8-sig','gb18030']):
        try:return raw.decode(enc).replace('\r\n','\n').replace('\r','\n')
        except UnicodeError:pass
    raise ValueError('Unsupported text encoding')
def backup(path,raw):
    key=sha(str(Path(path).resolve()).encode())[:24]
    name=DATA/'history'/key/(str(time.time_ns())+'.bak')
    atomic(name,raw)
    files=sorted(name.parent.glob('*.bak'))
    for old in files[:-30]:old.unlink()
    return str(name)
def bounded(root,name):
    root=Path(root).resolve();path=(root/name).resolve()
    if not path.is_relative_to(root) or path==root:raise ValueError('Path is outside configured workspace')
    if path.suffix.lower() not in ('.md','.markdown','.mdown','.mkd'):raise ValueError('Only Markdown files are allowed')
    return path

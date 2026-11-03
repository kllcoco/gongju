# Shared runtime for native GUI and MCP console entry.
from PyInstaller.utils.hooks import collect_submodules,copy_metadata
from pathlib import Path
base=str(Path(SPECPATH).resolve())
hidden=collect_submodules('markdown.extensions')+collect_submodules('reportlab.pdfbase')
a=Analysis([base+'/launcher.py'],pathex=[base],binaries=[],datas=[(base+'/assets/moxie.ico','assets'),(base+'/assets/moxie.png','assets')]+copy_metadata('mcp')+copy_metadata('markdown'),hiddenimports=hidden,hookspath=[],hooksconfig={},runtime_hooks=[],excludes=['pytest','IPython','matplotlib','numpy'],noarchive=False)
pyz=PYZ(a.pure)
gui=EXE(pyz,a.scripts,[],exclude_binaries=True,name='Moxie-2.0.4',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,icon=base+'/assets/moxie.ico')
mcp=EXE(pyz,a.scripts,[],exclude_binaries=True,name='MoxieMCP-2.0.4',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=True,icon=base+'/assets/moxie.ico')
coll=COLLECT(gui,mcp,a.binaries,a.datas,strip=False,upx=False,name='Moxie')

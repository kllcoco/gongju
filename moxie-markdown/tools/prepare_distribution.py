from pathlib import Path
import importlib.metadata
import json
import shutil

BASE = Path(__file__).resolve().parents[1]
DEST = BASE / 'dist' / 'Moxie'
if not (DEST / 'Moxie-2.0.2.exe').is_file():
    raise SystemExit('Build moxie.spec before preparing the distribution.')
(DEST / 'Documents').mkdir(exist_ok=True)
for name in ['使用说明.md', 'MCP接入说明.md', '示例.md']:
    shutil.copy2(BASE / name, DEST / name)
shutil.copy2(BASE / '示例.md', DEST / 'Documents' / '示例.md')
config = {'mcpServers': {'moxie-markdown': {
    'command': str(DEST / 'MoxieMCP-2.0.2.exe'),
    'args': ['--mcp', '--root', str(DEST / 'Documents')],
}}}
(DEST / 'mcp-config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
for distribution in importlib.metadata.distributions():
    for item in distribution.files or []:
        if '.dist-info' not in str(item):
            continue
        if not any(part.lower().startswith(('license', 'copying', 'notice')) for part in item.parts):
            continue
        source = Path(distribution.locate_file(item))
        if source.is_file():
            target = DEST / 'licenses' / distribution.metadata['Name'] / Path(*item.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
print('Prepared', DEST)

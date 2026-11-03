"""In-app documentation. Examples are shared with the exported manual."""
import json
import sys
from pathlib import Path
from core import BASE
VERSION = '2.0.5'
TOPICS = []
def add(category,title,steps,template='',language='markdown'):
    TOPICS.append({'id':str(len(TOPICS)),'category':category,'title':title,'steps':steps,'template':template,'language':language})

add('开始使用','关于墨写与快速开始','墨写是本地 Windows Markdown 编辑器。顶部保留新建、打开、保存、查找；左侧为文档大纲，中间为源码，右侧为阅读预览。\n第一次使用：新建 → 输入下面模板 → Ctrl+S → 选择 .md 文件。编辑约 0.2 秒后预览更新。语法插入请在源码区单击鼠标右键。\n在搜索框输入功能名，左侧选择条目；“复制模板”复制原样代码，“复制本节说明”复制步骤与模板。指南默认不改动当前文档；“用模板新建文档”会先询问如何处理未保存内容。', '# 我的第一篇文档\n\n这是一段正文，包含 **重点** 和 `代码`。\n\n## 今日计划\n\n- [ ] 阅读项目说明\n- [x] 创建 Markdown 文档\n')
add('开始使用','怎样使用右键菜单','有选中文字：先选中内容，再右键选择格式；选区会保留。\n没有选区：在希望插入的位置右键，光标会移到点击处。\nShift+F10：在当前光标处打开同一菜单。\n标题、列表、引用按整行处理；粗体、斜体等包裹选中文字。表格等块级模板会替换选区，使用前确认选中了需要替换的内容。','先写普通文字，选中“普通文字”，右键 → 文字格式 → 粗体。\n结果：先写**普通文字**。','text')
add('文件与保存','新建文档','点击顶部“新建”，或 文件 → 新建，或 Ctrl+N。\n当前内容有修改时会询问保存；取消则留在原文档。新文档首次保存需要选择路径。', '# 文档标题\n\n## 背景\n\n写下背景。\n\n## 内容\n\n写下主要内容。\n')
add('文件与保存','打开文件与最近文档','点击顶部“打开”、文件 → 打开，或 Ctrl+O，选择 .md / .markdown / 文本文件。\n支持 UTF-8、UTF-8 BOM、带 BOM 的 UTF-16，以及常见中文 GB18030 编码；单文件最大 20 MB。\n文件 → 最近打开，可重新打开最近 10 个路径。移动或删除原文件后，旧路径可能失效，请重新选择文件。','示例：打开 D:\\Notes\\README.md\n编辑后按 Ctrl+S 保存回原位置。','text')
add('文件与保存','保存与另存为','点击“保存”或 Ctrl+S，将编辑区写入磁盘。新文件会弹出保存位置选择框。\n文件 → 另存为，或 Ctrl+Shift+S，可以另存一份。原文件仍保留，后续保存作用于新路径。\n新文件、另存为使用 UTF-8；普通保存尽量保持原编码，新增字符无法用原编码表示时转为 UTF-8。普通保存已有文件会保留历史备份。','原文件：D:\\Notes\\计划.md\n另存为：D:\\Notes\\计划-修订版.md\n以后 Ctrl+S 保存的是“计划-修订版.md”。','text')
add('文件与保存','未保存提示与退出','标题中的 * 或“● 未保存”表示编辑区内容尚未写入磁盘。\n关闭窗口或 文件 → 退出：保存会写入文件，不保存会放弃当前窗口的修改及恢复草稿，取消会继续编辑。\n自动恢复草稿不能替代 Ctrl+S；需要交给别人使用的文件，应先手动保存。','完成编辑 → Ctrl+S → 确认标题没有“未保存” → 关闭窗口。','text')
add('编辑与查找','撤销、重做、剪切、复制、粘贴、全选','可从“编辑”菜单或源码区右键菜单使用这些操作。\nCtrl+Z 撤销，Ctrl+Y 重做；Ctrl+X 剪切，Ctrl+C 复制，Ctrl+V 粘贴，Ctrl+A 全选。\n撤销历史属于当前打开的编辑会话，关闭或重新加载文档后不能作为跨会话版本记录。历史内容请使用“文件 → 历史版本”。','误删文字 → Ctrl+Z\n撤销过头 → Ctrl+Y\n选中文字 → Ctrl+C → 在目标位置 Ctrl+V','text')
add('编辑与查找','查找、单个替换、全部替换','点击顶部“查找”，或 编辑 → 查找 / 替换，或 Ctrl+F / Ctrl+H。\n输入查找文字，点击“下一个”或在查找框按 Enter；查找会从光标处向后查找，末尾未找到时从开头继续。\n查找为字面文本、不区分大小写，不支持正则表达式。\n单个替换：先找到并选中匹配项，再填写“替换为”，点击“替换”。“全部替换”会替换整篇文档所有匹配项；可用 Ctrl+Z 撤销。','查找：TODO\n替换为：待办\n“下一个”定位一处；“替换”只替换选中的匹配项；“全部替换”处理整篇文档。','text')
add('编辑与查找','回车续写、缩进、退出列表','源码区 Tab 插入四个空格。\n列表行末按 Enter 自动延续列表；有序列表增加编号。任务行末回车产生新的未完成任务，即使上一项已完成。\n在只有列表标记、没有正文的空项上再次 Enter，结束该列表项。代码块中也可以直接按 Tab 或输入空格缩进。','1. 第一步\n2. 第二步\n\n- [x] 已完成的一步\n- [ ] 下一步\n')
add('阅读与导航','大纲跳转','使用 # 到 ###### 标题后，左侧大纲自动列出各级标题。\n单击大纲标题会同时定位源码和预览，并突出显示目标；阅读模式也可跳转。重复标题会按各自位置定位。\n也可在大纲中用方向键选择标题，再按 Enter。代码块里的 # 不会被当作文档标题。',' # 这一行前可有少量空格\n\n## 背景\n\n背景正文。\n\n## 实施方案\n\n方案正文。\n')
add('阅读与导航','双栏、专注编辑、阅读模式','视图 → 编辑 + 预览：同时显示源码和排版。\n视图 → 专注编辑：隐藏右侧预览。\n视图 → 阅读模式：隐藏源码，仅阅读预览。\n需要继续写作时切回双栏或专注编辑。预览区是只读的，不能直接改字或勾选任务。','写作时：视图 → 编辑 + 预览\n集中输入：视图 → 专注编辑\n检查成文：视图 → 阅读模式','text')
add('阅读与导航','字号、分栏与状态栏','视图 → 放大文字 / 缩小文字，可调整正文显示字号；也绑定了 Ctrl+加号 / Ctrl+减号。\n字号调整只改变显示，不修改 Markdown 文件的内容。预览的标题和代码使用各自样式。\n拖动各栏之间的分隔线调整宽度。底部显示字符数、行数、光标行列、编码和当前文件路径。','底部“光标 12.5”表示第 12 行、该行偏移 5 个字符的位置。\n查看完整文件路径可确认当前 Ctrl+S 会保存到哪里。','text')
add('Markdown 语法','标题 H1–H6 与恢复正文','右键 → 标题 → 选择一级到六级。也可直接输入 # 加一个空格。\n选中多行时逐行应用标题级别；已有 # 标题会切换级别。右键 → 标题 → 恢复正文会去掉 # 标题标记。\n同一文档通常用一个一级标题，其余按层级组织。','# 一级标题\n\n## 二级标题\n\n### 三级标题\n\n#### 四级标题\n\n##### 五级标题\n\n###### 六级标题\n')
add('Markdown 语法','下划线式标题','也支持在下一行使用 = 或 - 的标题写法。大纲会识别这些标题。\n下划线式标题仅有一级和二级；需要其他级别请使用 #。\n若要插入分隔线，应在 --- 前留空行，避免被识别成上一行的二级标题。','一级标题\n========\n\n二级标题\n--------\n')
add('Markdown 语法','粗体、斜体、粗斜体','选中文字后右键 → 文字格式，选择对应样式。无选区时会插入可替换的示例文字。\nCtrl+B 插入粗体，Ctrl+I 插入斜体。标记与要强调的文字之间不要留空格。','这是 **粗体**。\n\n这是 *斜体*。\n\n这是 ***粗斜体***。\n')
add('Markdown 语法','删除线','右键 → 文字格式 → 删除线，或用成对的 ~~ 包围文字。\n预览会显示删除线；文件本身保留原文字和标记。代码块、行内代码中的 ~~ 会按字面显示。','原计划：~~周五发布~~，现调整为周一发布。\n')
add('Markdown 语法','行内代码与反引号','右键 → 文字格式 → 行内代码。\n适合函数名、命令、路径、短代码。代码内容含反引号时，外层使用更多反引号；菜单会自动选择足够长的标记。','运行 `python app.py`。\n\n函数名是 `save_document()`。\n\n包含反引号的内容：`` `name` ``。\n')
add('Markdown 语法','无序列表与有序列表','右键 → 列表与任务 → 无序列表 / 有序列表。多行选区会逐行转换。\n无序项使用 - 加空格；有序项使用数字、点、空格。\n嵌套项通常缩进四个空格；列表前后留空行。','- 第一项\n- 第二项\n    - 第二项的子项\n\n1. 准备资料\n2. 执行操作\n3. 检查结果\n')
add('Markdown 语法','任务清单与已完成任务','右键 → 列表与任务 → 任务清单 / 已完成任务。\n未完成写作 - [ ]，已完成写作 - [x]，方括号后再加空格。\n在源码中把空格改成 x 即可标记完成；预览复选框只用于展示，不能点击修改。','- [ ] 编写说明\n- [x] 完成初稿\n- [ ] 检查并发布\n')
add('Markdown 语法','引用与嵌套引用','右键 → 引用 → 引用 / 嵌套引用。\n每行开头使用 > 加空格。嵌套引用使用 > >。\n引用可用于备注、摘录；引用结束后留一空行继续正文。','> 这是一段引用。\n> 第二行仍属于引用。\n>\n> > 这是嵌套引用。\n\n这里恢复正文。\n')
add('Markdown 语法','普通链接','右键 → 链接与图片 → 链接。把括号里的示例网址替换为实际地址。\n预览中点击 http:// 或 https:// 链接会用系统浏览器打开。当前不支持通过预览中的相对链接打开其他本地文档，也不保证 #锚点链接能在预览中跳转；章节定位请使用大纲。','[项目首页](https://github.com/)\n\n[带标题的链接](https://example.com "示例网站")\n')
add('Markdown 语法','引用式链接','右键 → 链接与图片 → 引用式链接。\n正文使用 [文字][标识]，文末单独定义 [标识]: URL。同一标识可以复用。\n菜单生成 link1、link2 等尚未使用的标识；修改文末的网址即可。','阅读 [官方网站][site]，也可以查看 [同一个网站][site]。\n\n[site]: https://example.com\n')
add('Markdown 语法','本地图片与图片语法','先保存 Markdown 文档，再右键 → 链接与图片 → 图片，选择本地图片。菜单会尽量插入相对路径，预览会显示图片。\n“图片语法”只插入文本模板，需自行填入真实路径。\n相对路径以 .md 文件所在文件夹为起点，建议用 / 分隔目录。下方模板要求同级 images 文件夹中已有 overview.png。网络图片不会自动下载；请先保存为本地文件。','![界面说明](images/overview.png)\n\n![文件名含空格的图片](<images/my overview.png>)\n')
add('Markdown 语法','表格与对齐','右键 → 表格，随后修改列名和单元格。第二行的 --- 不可省略。\n用冒号表示 Markdown 的左/中/右对齐；当前原生预览主要展示单元格网格，不保证呈现全部对齐样式。\n单元格需要竖线时写成 \\|；表格前后留空行。','| 项目 | 状态 | 数量 |\n| :--- | :---: | ---: |\n| 文档 | 已完成 | 3 |\n| 测试 | 进行中 | 2 |\n| A \\| B | 待确认 | 1 |\n')
add('Markdown 语法','分隔线','右键 → 分隔线。\n在独立一行写 ---，前后留空行。若紧贴上一行文字，可能被解析为下划线式二级标题。','第一部分内容。\n\n---\n\n第二部分内容。\n')
add('Markdown 语法','脚注','右键 → 脚注。菜单会在光标位置加入引用，在文末加入定义，并选择未使用的 note1、note2 等标识。\n把“脚注内容”替换为说明文字。引用与定义的标识必须一致。\n当前预览可以显示脚注内容；脚注内部锚点跳转不保证可用。','这句话需要补充说明。[^note1]\n\n[^note1]: 这里填写补充说明或来源。\n')
add('Markdown 语法','定义列表','右键 → 定义列表。术语单独一行，下一行用冒号加空格写解释。\n这是扩展语法；其他 Markdown 程序可能不支持。','Markdown\n: 一种轻量标记语言。\n\nMCP\n: 让客户端以标准方式调用工具的协议。\n')
add('Markdown 语法','段落与强制换行','空一行开始新段落。\n本程序的预览会显示普通换行；为便于其他 Markdown 阅读器正确换行，可在行尾加入两个空格，再换行。\n右键 → 强制换行会插入两个空格和换行符；两个空格肉眼不明显，复制模板时不要删掉。','第一行末尾有两个空格。  \n第二行仍在同一段。\n\n这是新的段落。\n')
add('Markdown 语法','转义特殊字符','选中文字后右键 → 转义特殊字符，会在 Markdown 特殊字符前加入反斜杠，使其按字面显示。\n想展示大段语法而不渲染时，优先使用代码块。','\\# 这不是标题\n\n\\*这不是斜体\\*\n\n显示方括号：\\[内容\\]\n')
add('代码块','代码块通用规则','右键 → 代码块 → 选择语言。普通代码块不加语言名；其他选项只设置语言标签，不代表提供彩色语法高亮。\n围栏必须独占一行，起止反引号数量应匹配。若内容本身含三个反引号，外层使用四个或更多；菜单会自动处理所选内容。','```\n这里是普通代码或原样文本。\n**这不会变成粗体**\n```\n')
for title,language,code in [
('Python','python','def hello(name):\n    return f"你好，{name}"\n\nprint(hello("墨写"))'),
('JavaScript','javascript','const message = "你好，墨写";\nconsole.log(message);'),
('TypeScript','typescript','const greet = (name: string): string => `你好，${name}`;\nconsole.log(greet("墨写"));'),
('JSON','json','{\n  "name": "墨写",\n  "enabled": true,\n  "items": ["文档", "笔记"]\n}'),
('Shell','bash','#!/usr/bin/env bash\nprintf "%s\\n" "Hello, Markdown"'),
('SQL','sql',"SELECT 'Hello, Markdown' AS message;"),
('HTML','html','<!doctype html>\n<html lang="zh-CN">\n  <meta charset="utf-8">\n  <title>示例页面</title>\n  <h1>你好，墨写</h1>\n</html>'),
('CSS','css','body {\n  font-family: sans-serif;\n  line-height: 1.8;\n}\nh1 { color: #137c70; }')]:
    add('代码块',title+' 代码块',f'右键 → 代码块 → {title}。复制下方模板到 Markdown 文档，再修改代码内容。\n这些代码在编辑器中只作为文本展示，不会执行。',f'```{language}\n{code}\n```\n')
add('导出与恢复','导出 HTML','文件 → 导出 HTML，选择输出文件。\n导出的是当前编辑区内容，包括未保存修改；导出不会代替保存 .md 原文。\nHTML 会包含基础排版，复杂样式与原生预览可能不同。相对图片路径仍需相应图片文件，建议与原 .md 放在相同目录，或调整图片路径后再分发。','编辑文档 → 文件 → 导出 HTML → 保存为 说明.html\n交付时同时提供文档引用的 images 文件夹。','text')
add('导出与恢复','导出 PDF','文件 → 导出 PDF，选择输出路径。\nPDF 从当前编辑区生成，支持中文、基本表格和本地图片。导出不会修改 .md 原文。\nPDF 使用简洁排版，并非逐像素复制预览；复杂 HTML、行内样式、超长表格和特殊符号可能需要另行调整。','检查图片路径 → 文件 → 导出 PDF → 保存为 说明.pdf\n打开导出的 PDF，检查分页和图片后再分享。','text')
add('导出与恢复','自动恢复草稿','编辑后约每两秒保存一份恢复草稿，不自动覆盖原文件。异常退出后重启，程序会提示发现草稿。\n也可以 文件 → 恢复草稿，选择条目，点击恢复；恢复后先检查内容，再 Ctrl+S。\n恢复列表不包含仍在运行的其他窗口；最近不足两秒的输入可能尚未写入草稿。正常退出时选择“不保存”会清除当前窗口的草稿。','异常退出 → 重启程序 → 查看恢复草稿 → 选择文档 → 恢复 → 核对内容 → Ctrl+S。','text')
add('导出与恢复','历史版本','普通保存已有文件前会备份磁盘旧内容，每个文件保留最近 30 次备份。\n文件 → 历史版本，按时间选择，点击“恢复到编辑区”。恢复不会立即覆盖文件；确认后 Ctrl+S。\n首次新建文件、另存为和从未保存的内容，不应依赖此功能恢复。','打开目标文件 → 文件 → 历史版本 → 选定时间 → 恢复到编辑区 → 检查 → Ctrl+S。','text')
add('导出与恢复','外部修改与版本冲突','其他程序或 MCP 修改了磁盘文件时：若本窗口没有未保存修改，会在约两秒内自动加载；若有修改，会保留当前编辑内容并提示。\n再次保存时如发现磁盘内容变化，会询问是否覆盖。需要保留两边时先取消覆盖，用“另存为”保留当前内容，再人工合并。','遇到冲突 → 取消覆盖 → Ctrl+Shift+S 保存为 文档-我的修改.md → 对照原文件合并。','text')
add('快捷键与排错','全部快捷键','以下快捷键作用于编辑器窗口；在原生文件对话框内，键盘由对话框处理。\n若某个快捷键被输入法或系统占用，可使用对应的顶部按钮、菜单或右键项。','| 快捷键 | 用途 |\n| --- | --- |\n| Ctrl+N | 新建 |\n| Ctrl+O | 打开 |\n| Ctrl+S | 保存 |\n| Ctrl+Shift+S | 另存为 |\n| Ctrl+F / Ctrl+H | 查找替换 |\n| Ctrl+B / Ctrl+I | 粗体 / 斜体 |\n| Ctrl+Z / Ctrl+Y | 撤销 / 重做 |\n| Ctrl+X / C / V / A | 剪切 / 复制 / 粘贴 / 全选 |\n| Ctrl+加号 / 减号 | 调整显示字号 |\n| Tab | 插入四个空格 |\n| Shift+F10 | 当前光标处打开右键菜单 |\n')
add('快捷键与排错','常见问题与功能边界','文字未排版：确认在源码区输入，检查标记两侧是否配对、标题后是否有空格、代码围栏是否闭合。\n大纲没有项目：使用 # 标题，普通文字不是标题。\n图片没显示：确认是本地文件、路径正确；先保存 .md，再重新插入图片。\n中文乱码：尝试把源文件转换为 UTF-8；不要直接覆盖仍未正确识别的原文件。\n程序移动后无法启动：保留 EXE 与整个 _internal 文件夹；桌面快捷方式及 MCP command 也需更新路径。\n当前不支持阅读区直接编辑、LaTeX、Mermaid、网络图片自动下载、协同编辑或 DOCX 导出。','排查顺序：确认当前文件路径 → 检查源码语法 → 保存为 UTF-8 副本 → 重新打开并预览。','text')
add('完整文档模板','项目 README 模板','复制后粘贴到新文档，保存为 README.md。将项目名称、运行命令和清单替换成实际内容。','# 项目名称\n\n## 简介\n\n一句话说明项目用途。\n\n## 安装\n\n```bash\npython -m pip install -r requirements.txt\n```\n\n## 使用\n\n```bash\npython app.py\n```\n\n## 功能\n\n- [x] 基础功能\n- [ ] 后续计划\n\n## 常见问题\n\n### 如何启动？\n\n按照“使用”章节操作。\n')
add('完整文档模板','会议纪要模板','复制后填写主题、参与人和结论。任务清单可在源码中把 [ ] 改为 [x]。','# 会议纪要\n\n- 日期：填写会议日期\n- 主题：填写会议主题\n- 参与人：填写参与人\n\n## 讨论要点\n\n1. 第一项\n2. 第二项\n\n## 决定\n\n> 写下已经确认的结论。\n\n## 待办\n\n| 事项 | 负责人 | 截止时间 |\n| --- | --- | --- |\n| 完成初稿 | 张三 | 填写日期 |\n\n- [ ] 跟进结果\n')
add('完整文档模板','学习笔记模板','复制后作为一篇新的学习笔记，使用大纲快速切换章节。','# 学习主题\n\n## 核心概念\n\n**重点：** 用自己的话说明。\n\n## 示例\n\n```python\nprint("Hello, world")\n```\n\n## 易错点\n\n- 第一处易错点\n- 第二处易错点\n\n## 总结\n\n写下本次学习的收获。\n\n## 下一步\n\n- [ ] 动手实践\n')

def mcp_config():
    root=BASE/'Documents'
    if getattr(sys,'frozen',False):
        try:
            installed=json.loads((BASE/'mcp-config.json').read_text('utf-8-sig'))['mcpServers']['moxie-markdown']
            if isinstance(installed.get('command'),str) and isinstance(installed.get('args'),list) and all(isinstance(a,str) for a in installed['args']):
                return {'mcpServers':{'moxie-markdown':{'command':installed['command'],'args':installed['args']}}}
        except (OSError,ValueError,KeyError,TypeError):pass
        command=str(BASE/f'MoxieMCP-{VERSION}.exe');args=['--mcp','--root',str(root)]
    else:command=sys.executable;args=[str(BASE/'mcp_server.py'),'--root',str(root)]
    return {'mcpServers':{'moxie-markdown':{'command':command,'args':args}}}
config=mcp_config()
add('MCP 与 LLM','MCP 接入配置','文件 → 打开 MCP 配置说明，可以查看另一份接入说明。将下方 moxie-markdown 配置合并到客户端的 mcpServers；不要覆盖已有其他服务。\n配置优先读取本机已安装的 MCP 入口；未发现配置时按当前程序位置生成。--root 后面的目录是允许读写的 Markdown 工作目录，可改成自己的绝对路径。移动程序后需重新复制配置并重载客户端。\n这是 stdio MCP，不需要端口或 API 密钥；是否允许调用由 LLM 客户端控制。',json.dumps(config,ensure_ascii=False,indent=2),'json')
add('MCP 与 LLM','给 LLM 的直接指令模板','可把以下自然语言指令直接发给已接入 MCP 的 LLM 客户端。\n文件工具直接写磁盘；编辑区更新工具只产生未保存的修改。指定“先不要保存”可先审阅。','读取墨写当前窗口的文档，改进表达，保持事实和标题层级不变。先读取最新 version，再把结果放回编辑区，不要保存。\n\n在墨写工作目录中新建 notes/计划.md，写一份项目计划，并在独立编辑器中打开。\n\n读取墨写当前窗口的最新内容和 version，确认后保存；若发现版本冲突，停止覆盖并告诉我。','text')
add('MCP 与 LLM','工具模板的正确使用顺序','下面各工具的 JSON 模板是 MCP tools/call 的 params，外层 JSON-RPC 请求由客户端生成。\n读取返回的 version 是 SHA-256 值。写入模板中的 <文件version> 或 <编辑区version> 必须替换为对应读取结果，不能原样调用。\n文件版本与编辑区版本不能混用；修改后 version 会变化。多个窗口时先获取 session_id，再使用具体窗口的 ID。\n所有已保存文档路径必须在 --root 内；未命名文档可改编辑区，但第一次保存须在界面选择位置。','文件：read_document → 使用返回的 version 调用 write_document 或 replace_text。\n窗口：list_editor_sessions → get_editor_document → update_editor_document → 审阅 → get_editor_document → save_editor_document。','text')
def tool(title,name,steps,arguments):
    add('MCP 工具逐项用法',title,steps+'\n模板是 tools/call 的 params；含尖括号的值需要替换为前一步实际返回值。',json.dumps({'name':name,'arguments':arguments},ensure_ascii=False,indent=2),'json')
tool('workspace_info · 查看工作目录','workspace_info','接入后首先调用，确认 root、支持的扩展名和文件大小限制。',{})
tool('list_documents · 列出文件','list_documents','按相对路径中的文字筛选，query 为空时列出全部。limit 最多 500；truncated=true 表示结果被截断。',{'query':'','limit':100})
tool('read_document · 读取文件','read_document','path 是相对 --root 的路径。返回 text 和磁盘 version；保存好 version 供后续写入使用。',{'path':'notes/计划.md'})
tool('create_document · 新建文件','create_document','创建 UTF-8 Markdown 文件，并按需要创建子文件夹。同名文件存在时拒绝覆盖。',{'path':'notes/新计划.md','text':'# 项目计划\n\n- [ ] 完成初稿\n'})
tool('write_document · 写入整篇文件','write_document','先调用 read_document。只有当前磁盘版本与 expected_version 一致才会写入，并备份旧文件。版本不一致时重新读取，不要盲目重试。',{'path':'notes/计划.md','text':'# 项目计划\n\n- [x] 完成初稿\n','expected_version':'<文件version>'})
tool('replace_text · 精确替换','replace_text','先读取文件及 version。old_text 是精确字面文本；expected_occurrences 必须与实际匹配次数相同，默认为 1。此工具区分大小写，与界面查找不同。',{'path':'notes/计划.md','old_text':'完成初稿','new_text':'检查初稿','expected_version':'<文件version>','expected_occurrences':1})
tool('open_in_editor · 打开窗口','open_in_editor','在新的独立编辑窗口中打开工作目录内已有的 Markdown 文件，返回进程编号。文件必须已存在。',{'path':'notes/计划.md'})
tool('list_editor_sessions · 列出窗口','list_editor_sessions','获取正在运行的编辑器 session_id。多个窗口时必须选择目标窗口，不能猜测。',{})
tool('get_editor_document · 读编辑区','get_editor_document','读取当前编辑区（包括未保存修改）、dirty 状态及编辑区 version。只有一个窗口时 session_id 可以省略。',{'session_id':'<目标窗口session_id>'})
tool('update_editor_document · 改编辑区','update_editor_document','先读取目标编辑区；使用编辑区 version。修改可撤销，并保持未保存状态，供用户审阅。不会自动保存到磁盘。',{'session_id':'<目标窗口session_id>','expected_version':'<编辑区version>','text':'# 修订后的计划\n\n- [ ] 确认内容\n'})
tool('save_editor_document · 保存编辑区','save_editor_document','在用户已要求保存后，读取最新编辑区 version 再调用。当前文档必须已有路径；编辑区或磁盘版本发生变化会拒绝保存。',{'session_id':'<目标窗口session_id>','expected_version':'<编辑区version>'})

def manual_markdown(portable=False):
    lines=[f'# 墨写 {VERSION} · 详细使用指南','', '在程序中选择“帮助 → 关于与详细用法”，可搜索功能并一键复制模板。','']
    category=None
    for topic in TOPICS:
        if topic['category']!=category:category=topic['category'];lines += ['## '+category,'']
        lines += ['### '+topic['title'],'',topic['steps'],'']
        if topic['template']:
            template=topic['template']
            if portable and topic['title']=='MCP 接入配置':
                template=json.dumps({'mcpServers':{'moxie-markdown':{'command':f'C:/Tools/Moxie/MoxieMCP-{VERSION}.exe','args':['--mcp','--root','D:/Notes']}}},ensure_ascii=False,indent=2)
            runs=__import__('re').findall(r'`+',template);fence='`'*max(4,max(map(len,runs),default=0)+1)
            lines += [fence+topic['language'],template.rstrip('\n'),fence,'']
    return '\n'.join(lines)

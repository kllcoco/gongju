"""Source locations matched to rendered Markdown headings, including repeated names."""
import re
from functools import lru_cache
import markdown
from rendering import Tree
from writing_extension import WritingExtension

def source_headings(text):
    lines=text.splitlines();fence=None
    for i,line in enumerate(lines):
        marker=re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$',line)
        if marker:
            run=marker[1]
            if fence:
                if run[0]==fence[0] and len(run)>=len(fence) and not marker[2].strip():fence=None
            else:fence=run
            continue
        if fence:continue
        fragment=None;number=i+1
        if re.match(r'^ {0,3}#{1,6}(?!#)',line):fragment=line
        elif i>0 and lines[i-1].strip() and re.match(r'^ {0,3}(=+|-+)\s*$',line):fragment=lines[i-1]+'\n'+line;number=i
        if fragment is None:continue
        found=parse_heading(fragment)
        if found:yield (number,)+found

@lru_cache(maxsize=4096)
def parse_heading(fragment):
    # Cached: re-rendering on each keystroke would otherwise run Markdown once per heading.
    nodes=Tree(markdown.markdown(fragment,extensions=['extra',WritingExtension()])).root.children
    heading=next((n for n in nodes if not isinstance(n,str) and n.tag in ('h1','h2','h3','h4','h5','h6')),None)
    return (int(heading.tag[1]),heading.text().strip()) if heading else None

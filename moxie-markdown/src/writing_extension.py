"""Markdown additions used consistently by preview and HTML export."""
import re
from xml.etree import ElementTree as ET
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from markdown.treeprocessors import Treeprocessor
class Strike(InlineProcessor):
    def handleMatch(self,m,data):
        node=ET.Element('del');node.text=m.group(1);return node,m.start(0),m.end(0)
class Tasks(Treeprocessor):
    def run(self,root):
        for item in root.iter('li'):
            target=item.find('p')
            if target is None:target=item
            text=target.text or '';match=re.match(r'^\[([ xX])\]\s+',text)
            if match:
                box=ET.Element('input',{'type':'checkbox','disabled':'disabled'})
                if match[1].lower()=='x':box.set('checked','checked')
                target.text='';box.tail=text[match.end():];target.insert(0,box)
class WritingExtension(Extension):
    def extendMarkdown(self,md):
        md.inlinePatterns.register(Strike(r'(?<!~)~~(?=\S)(.+?)(?<=\S)~~(?!~)',md),'moxie-strike',175)
        md.treeprocessors.register(Tasks(md),'moxie-tasks',15)

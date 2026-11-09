# 把「目标效果.src.html」编译成单文件版：三张效果图压成 webp 内嵌，双击就能看
# 源文件改完跑一次： python dev/build-目标效果.py
import base64, io, json, os, re
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'dev', '目标效果.src.html')
OUT = os.path.join(ROOT, '作品集', 'figma', '目标效果.html')
KEYS = {'01-屏@2x.png': '01', '02-屏@2x.png': '02', '03-屏@2x.png': '03'}

html = open(SRC, encoding='utf-8').read()

uris = {}
for name, key in KEYS.items():
    path = os.path.join(ROOT, '作品集', 'figma', name)
    im = Image.open(path).convert('RGB').resize((1440, 900), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'WEBP', quality=92, method=6)
    uris[key] = 'data:image/webp;base64,' + base64.b64encode(buf.getvalue()).decode()

for name, key in KEYS.items():
    html = html.replace('data-full="%s"' % name, 'data-k="%s"' % key)
    html = html.replace('<img src="%s"' % name, '<img data-k="%s"' % key)

html = html.replace('<meta name="viewport"',
                    '<meta name="color-scheme" content="light">\n<meta name="viewport"')
html = html.replace(':root{\n    --bg:', ':root{\n    color-scheme:light;\n    --bg:')
html = html.replace('<body>', '<body style="background:#f5f6f8;color:#12141a">')

inject = ('<script>\nconst IMG=%s;\n'
          'document.querySelectorAll("[data-k]").forEach(function(el){\n'
          '  var u=IMG[el.dataset.k]; if(!u) return;\n'
          '  if(el.tagName==="IMG"){el.src=u;} else {el.dataset.full=u;}\n'
          '});\n</script>\n') % json.dumps(uris, ensure_ascii=False)
html = html.replace('</body>', inject + '</body>')

open(OUT, 'w', encoding='utf-8').write(html)
print('written', OUT, round(os.path.getsize(OUT) / 1024), 'KB')
print('外链残留:', set(re.findall(r'(?:src|data-full)="(?!data:)([^"]*)"', html)))

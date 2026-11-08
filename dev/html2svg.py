"""把三屏设计稿 HTML 导出成 Figma 能直接拖进去的 SVG。

Figma 导入 SVG 时会生成可编辑的矢量图层和文字图层（不是死图），
效果和 html.to.design 插件接近——插件也是绝对定位图层。
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "作品集" / "figma" / "三屏设计稿.html"
OUT = ROOT / "作品集" / "figma" / "svg"

JS = r"""
(idx) => {
  const screenEl = document.querySelectorAll('.screen')[idx];
  const sr = screenEl.getBoundingClientRect();

  const hex = (c) => {
    const m = String(c).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const fill = (c) => '#' + [c.r, c.g, c.b].map(v =>
    Math.round(v).toString(16).padStart(2, '0')).join('');
  const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const body = [];
  let gradId = 0;

  const radius = (el, cs, w, h) => {
    const r = cs.borderTopLeftRadius;
    if (!r) return 0;
    if (r.indexOf('%') >= 0) return Math.min(w, h) / 2;
    return Math.min(parseFloat(r) || 0, Math.min(w, h) / 2);
  };

  // 渐变只处理最简形式：linear-gradient(Ndeg, c1, c2)
  // 注意不能直接 split(',')：rgb(51, 88, 216) 里的逗号会把它切碎
  const splitArgs = (s) => {
    const out = [];
    let depth = 0, cur = '';
    for (const ch of s) {
      if (ch === '(') depth++;
      if (ch === ')') depth--;
      if (ch === ',' && depth === 0) { out.push(cur.trim()); cur = ''; }
      else cur += ch;
    }
    if (cur.trim()) out.push(cur.trim());
    return out;
  };

  const gradient = (bgImg, el, cs, w, h, ox, oy) => {
    if (!bgImg || bgImg.indexOf('linear-gradient') < 0) return null;
    const inner = bgImg.slice(bgImg.indexOf('(') + 1, bgImg.lastIndexOf(')'));
    const parts = splitArgs(inner);
    if (parts.length < 3) return null;
    const ang = parseFloat(parts[0]) || 180;
    const c1 = hex(parts[1].replace(/\s+\d+%$/, ''));
    const c2 = hex(parts[parts.length - 1].replace(/\s+\d+%$/, ''));
    if (!c1 || !c2) return null;
    const rad = (ang - 90) * Math.PI / 180;
    const cx = ox + w / 2, cy = oy + h / 2;
    const dx = Math.cos(rad) * w / 2, dy = Math.sin(rad) * h / 2;
    gradId += 1;
    const id = 'g' + gradId;
    return {
      id,
      def: `<linearGradient id="${id}" x1="${cx - dx}" y1="${cy - dy}" x2="${cx + dx}" y2="${cy + dy}" gradientUnits="userSpaceOnUse">`
        + `<stop offset="0" stop-color="${fill(c1)}"/><stop offset="1" stop-color="${fill(c2)}"/></linearGradient>`,
      paint: `url(#${id})`,
    };
  };

  const linesOf = (node) => {
    const range = document.createRange();
    const len = node.textContent.length;
    const out = [];
    let cur = null;
    for (let i = 0; i < len; i++) {
      range.setStart(node, i);
      range.setEnd(node, i + 1);
      const rs = range.getClientRects();
      if (!rs.length) continue;
      const r = rs[0];
      if (!cur || Math.abs(cur.top - r.top) > 2) {
        cur = { top: r.top, bottom: r.bottom, left: r.left, right: r.right, text: '' };
        out.push(cur);
      }
      cur.text += node.textContent[i];
      cur.left = Math.min(cur.left, r.left);
      cur.right = Math.max(cur.right, r.right);
      cur.bottom = Math.max(cur.bottom, r.bottom);
    }
    return out.filter(l => l.text.trim());
  };

  const emitText = (node, el, cs) => {
    const lines = linesOf(node);
    if (!lines.length) return;
    const fs = parseFloat(cs.fontSize) || 14;
    const weight = parseInt(cs.fontWeight) || 400;
    const c = hex(cs.color);
    if (!c || c.a < 0.02) return;
    const ls = cs.letterSpacing === 'normal' ? 0 : parseFloat(cs.letterSpacing) || 0;
    const align = cs.textAlign;

    for (const ln of lines) {
      // Range 取到的是客户端绝对坐标，必须减掉画板原点，否则整段文字会偏移
      const top = ln.top - sr.top;
      const h = ln.bottom - ln.top;
      // 文字基线：行盒里字体大致居中，基线下沉约 0.80em
      const y = top + h / 2 + fs * 0.355;
      const left = ln.left - sr.left;
      const right = ln.right - sr.left;
      let x = left, anchor = 'start';
      if (align === 'center') { x = (left + right) / 2; anchor = 'middle'; }
      else if (align === 'right' || align === 'end') { x = right; anchor = 'end'; }
      body.push(
        `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" font-family="Microsoft YaHei"`
        + ` font-size="${fs}" font-weight="${weight}"`
        + (ls ? ` letter-spacing="${ls}"` : '')
        + ` fill="${fill(c)}" text-anchor="${anchor}"`
        + (c.a < 0.99 ? ` fill-opacity="${c.a.toFixed(2)}"` : '')
        + `>${esc(ln.text)}</text>`);
    }
  };

  const walk = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'style' || tag === 'script') return;
    const r = el.getBoundingClientRect();
    if (r.width < 0.5 || r.height < 0.5) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if (parseFloat(cs.opacity) < 0.02) return;
    if (r.right < sr.left || r.left > sr.right) return;

    const rx = radius(el, cs, r.width, r.height);
    const x = r.left - sr.left, y = r.top - sr.top, w = r.width, h = r.height;

    // 内联 SVG 图标：整块搬过去，保持矢量
    // 不用嵌套 <svg>（Figma 导入不稳），改成 g + translate/scale，扁平化
    if (tag === 'svg') {
      const inner = el.innerHTML;
      const vb = (el.getAttribute('viewBox') || `0 0 ${w} ${h}`).trim().split(/[\s,]+/).map(Number);
      const vx = vb[0] || 0, vy = vb[1] || 0, vw = vb[2] || w, vh = vb[3] || h;
      const s = Math.min(w / vw, h / vh);
      body.push(`<g transform="translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${s.toFixed(4)}) translate(${-vx},${-vy})"`
        + ` fill="${el.getAttribute('fill') || 'none'}"`
        + ` stroke="${el.getAttribute('stroke') || 'none'}"`
        + ` stroke-width="${el.getAttribute('stroke-width') || 0}"`
        + ` stroke-linecap="round" stroke-linejoin="round">${inner}</g>`);
      return;
    }

    const bgImg = cs.backgroundImage;
    const g = gradient(bgImg, el, cs, w, h, x, y);
    const bg = hex(cs.backgroundColor);
    if (g) {
      body.push(g.def);
      body.push(`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}"`
        + ` rx="${rx.toFixed(1)}" fill="${g.paint}"/>`);
    } else if (bg && bg.a > 0.01) {
      body.push(`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}"`
        + ` rx="${rx.toFixed(1)}" fill="${fill(bg)}"`
        + (bg.a < 0.99 ? ` fill-opacity="${bg.a.toFixed(2)}"` : '') + `/>`);
    }

    // 描边：逐边画，避免整圈描边把单边 border 画成方框
    const sides = [
      ['Top', x, y, w, 0],
      ['Right', x + w - 0.5, y, 0, h],
      ['Bottom', x, y + h - 0.5, w, 0],
      ['Left', x + 0.5, y, 0, h],
    ];
    for (const [side, bx, by, bw, bh] of sides) {
      const bwd = parseFloat(cs['border' + side + 'Width']) || 0;
      const bc = hex(cs['border' + side + 'Color']);
      if (bwd < 0.4 || !bc || bc.a < 0.02) continue;
      if (bw === 0) {
        body.push(`<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="0.8" height="${bh.toFixed(1)}" fill="${fill(bc)}"/>`);
      } else {
        body.push(`<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${bw.toFixed(1)}" height="0.8" fill="${fill(bc)}"/>`);
      }
    }

    for (const node of el.childNodes) {
      if (node.nodeType === 3 && node.textContent.trim()) emitText(node, el, cs);
    }
    for (const child of el.children) walk(child);
  };

  walk(screenEl);
  const w = Math.round(sr.width), h = Math.round(sr.height);
  const defs = body.filter(s => s.indexOf('<linearGradient') >= 0).join('');
  const shapes = body.filter(s => s.indexOf('<linearGradient') < 0).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">`
    + (defs ? `<defs>${defs}</defs>` : '')
    + `<rect width="${w}" height="${h}" fill="#F4F5F7"/>`
    + shapes + `</svg>`;
}
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    names = ["01-首页.svg", "02-对练中.svg", "03-复盘.svg"]
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge")
        pg = b.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
        pg.goto(SRC.resolve().as_uri(), wait_until="networkidle")
        pg.wait_for_timeout(400)
        for i, name in enumerate(names):
            svg = pg.evaluate(JS, i)
            path = OUT / name
            path.write_text(svg, encoding="utf-8")
            print(name, f"{len(svg)/1024:.1f} KB",
                  "text节点", svg.count("<text"), "rect", svg.count("<rect"))
        b.close()

    # 反向校验：把导出的 SVG 再渲染回图片，和 HTML 截图对比
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge")
        pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        for name in names:
            svg = (OUT / name).read_text(encoding="utf-8")
            html = ("<!DOCTYPE html><meta charset='utf-8'>"
                    "<style>html,body{margin:0;padding:0;overflow:hidden}</style>" + svg)
            tmp = OUT / "_wrap.html"
            tmp.write_text(html, encoding="utf-8")
            pg.goto(tmp.as_uri())
            pg.screenshot(path=str(OUT / ("_check-" + name.replace(".svg", ".png"))))
            tmp.unlink()
        b.close()
    print("ok")


if __name__ == "__main__":
    sys.exit(main())

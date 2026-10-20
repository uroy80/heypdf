// A tiny, safe Markdown subset for AI output (headings, paragraphs, bullet/numbered lists, quotes, code, rules,
// **bold**, *italic*, `code`). Model output is untrusted: parse to plain data, then build DOM nodes — never innerHTML.

export function parseInline(text) {
  const out = [];
  // intraword * and _ stay literal (snake_case names, 2*3*4)
  const re = /(\*\*([^*]+)\*\*|(?<![\w_])__([^_]+)__(?![\w_])|`([^`]+)`|(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])|(?<![\w_])_([^_\s][^_]*?)_(?![\w_]))/g;
  let last = 0;
  let m;
  const src = String(text ?? '');
  while ((m = re.exec(src))) {
    if (m.index > last) out.push({ t: 'text', v: src.slice(last, m.index) });
    if (m[2] != null || m[3] != null) out.push({ t: 'b', v: m[2] ?? m[3] });
    else if (m[4] != null) out.push({ t: 'code', v: m[4] });
    else out.push({ t: 'i', v: m[5] ?? m[6] });
    last = re.lastIndex;
  }
  if (last < src.length) out.push({ t: 'text', v: src.slice(last) });
  return out;
}

/** Markdown → blocks: {type:'h', level, inline} · {type:'p', inline} · {type:'ul'|'ol', items:[inline]} · {type:'quote', inline} · {type:'code', text} · {type:'hr'}. */
export function parseMarkdown(text) {
  const blocks = [];
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  let para = [];
  let list = null;
  let code = null;
  const flushPara = () => {
    if (para.length) blocks.push({ type: 'p', inline: parseInline(para.join(' ')) });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push(list);
    list = null;
  };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (code) {
      if (/^```/.test(line)) {
        blocks.push({ type: 'code', text: code.join('\n') });
        code = null;
      } else code.push(raw);
      continue;
    }
    if (/^```/.test(line)) {
      flushPara();
      flushList();
      code = [];
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    let m = /^(#{1,6})\s+(.*)$/.exec(line);
    if (m) {
      flushPara();
      flushList();
      blocks.push({ type: 'h', level: m[1].length, inline: parseInline(m[2].replace(/\s#+$/, '')) });
      continue;
    }
    if (/^(\*\s*\*\s*\*|-\s*-\s*-|_\s*_\s*_)[\s*_-]*$/.test(line)) {
      flushPara();
      flushList();
      blocks.push({ type: 'hr' });
      continue;
    }
    m = /^\s*([-*+•])\s+(.*)$/.exec(line) || null;
    const n = m ? null : /^\s*(\d{1,3})[.)]\s+(.*)$/.exec(line);
    if (m || n) {
      flushPara();
      const type = m ? 'ul' : 'ol';
      if (!list || list.type !== type) {
        flushList();
        list = { type, items: [] };
      }
      list.items.push(parseInline((m || n)[2]));
      continue;
    }
    m = /^>\s?(.*)$/.exec(line);
    if (m) {
      flushPara();
      flushList();
      blocks.push({ type: 'quote', inline: parseInline(m[1]) });
      continue;
    }
    if (list) {
      // a continuation line of the last list item
      const items = list.items;
      items[items.length - 1] = [...items[items.length - 1], { t: 'text', v: ' ' }, ...parseInline(line.trim())];
      continue;
    }
    para.push(line.trim());
  }
  if (code) blocks.push({ type: 'code', text: code.join('\n') });
  flushPara();
  flushList();
  return blocks;
}

function inlineNodes(doc, inline) {
  return inline.map((s) => {
    if (s.t === 'text') return doc.createTextNode(s.v);
    const el = doc.createElement(s.t === 'b' ? 'strong' : s.t === 'i' ? 'em' : 'code');
    el.textContent = s.v;
    return el;
  });
}

/** Render blocks into `el` (replacing its children). Headings are shifted down so they nest under the page's h2. */
export function renderMarkdown(el, text, { headingBase = 3 } = {}) {
  const doc = el.ownerDocument;
  const nodes = [];
  for (const b of parseMarkdown(text)) {
    let node;
    if (b.type === 'h') {
      node = doc.createElement(`h${Math.min(6, headingBase + b.level - 1)}`);
      node.append(...inlineNodes(doc, b.inline));
    } else if (b.type === 'p' || b.type === 'quote') {
      node = doc.createElement(b.type === 'p' ? 'p' : 'blockquote');
      node.append(...inlineNodes(doc, b.inline));
    } else if (b.type === 'ul' || b.type === 'ol') {
      node = doc.createElement(b.type);
      for (const it of b.items) {
        const li = doc.createElement('li');
        li.append(...inlineNodes(doc, it));
        node.append(li);
      }
    } else if (b.type === 'code') {
      node = doc.createElement('pre');
      const c = doc.createElement('code');
      c.textContent = b.text;
      node.append(c);
    } else {
      node = doc.createElement('hr');
    }
    nodes.push(node);
  }
  el.replaceChildren(...nodes);
}

// A small DOM for node tests of heypdf views (no dependencies): elements, text, attributes, classList, dataset,
// style, events (with bubbling), focus, simple CSS selectors (tag, #id, .class, [attr], [attr="v"], descendant
// combinator, comma lists, :not(), :disabled). Not a browser — just enough for the views' DOM code paths.
// Usage: const dom = installDom(); … dom.uninstall();

const kebab = (s) => s.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);

class Node {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
    this._listeners = new Map();
  }

  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === this.ownerDocument;
  }

  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) {
    this._detachAll();
    if (v !== '' && v != null) this._adopt(new Text(this.ownerDocument, String(v)));
  }

  _adopt(node) {
    if (typeof node === 'string') node = new Text(this.ownerDocument, node);
    node.remove?.();
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  _detachAll() {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
  }

  append(...nodes) { for (const n of nodes) this._adopt(n); }
  appendChild(n) { return this._adopt(n); }
  prepend(...nodes) {
    const add = nodes.map((n) => (typeof n === 'string' ? new Text(this.ownerDocument, n) : n));
    for (const n of add) n.remove?.();
    for (const n of add) n.parentNode = this;
    this.childNodes.unshift(...add);
  }
  replaceChildren(...nodes) {
    this._detachAll();
    this.append(...nodes);
  }
  remove() {
    if (!this.parentNode) return;
    const list = this.parentNode.childNodes;
    const i = list.indexOf(this);
    if (i >= 0) list.splice(i, 1);
    this.parentNode = null;
  }
  contains(n) {
    for (let x = n; x; x = x.parentNode) if (x === this) return true;
    return false;
  }

  addEventListener(type, fn, opts) {
    if (!fn) return;
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push({ fn, once: Boolean(opts && opts.once) });
  }
  removeEventListener(type, fn) {
    const l = this._listeners.get(type);
    if (l) this._listeners.set(type, l.filter((x) => x.fn !== fn));
  }
  dispatchEvent(ev) {
    if (!ev.target) ev.target = this;
    const path = [];
    for (let n = this; n; n = n.parentNode) path.push(n);
    if (ev.bubbles && this.isConnected && this.ownerDocument.defaultView) path.push(this.ownerDocument.defaultView);
    for (const n of ev.bubbles ? path : [this]) {
      ev.currentTarget = n;
      for (const l of [...(n._listeners.get(ev.type) || [])]) {
        if (l.once) n.removeEventListener(ev.type, l.fn);
        l.fn.call(n, ev);
      }
      if (ev._stop) break;
    }
    return !ev.defaultPrevented;
  }
}

class Text extends Node {
  constructor(doc, data) {
    super(doc);
    this.nodeType = 3;
    this.data = data;
  }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

function makeStyle() {
  const style = { cssText: '' };
  return new Proxy(style, {
    set(t, k, v) {
      t[k] = v;
      return true;
    },
    get(t, k) {
      if (k === 'setProperty') return (name, v) => { t[name] = v; };
      return t[k] ?? '';
    },
  });
}

export class Event {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    Object.assign(this, init);
    this.defaultPrevented = false;
    this.target = null;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; }
}

class Element extends Node {
  constructor(doc, tag, ns = null) {
    super(doc);
    this.nodeType = 1;
    this.namespaceURI = ns;
    this.tagName = ns ? tag : tag.toUpperCase();
    this.localName = tag.toLowerCase();
    this._attrs = new Map();
    this.style = makeStyle();
    const el = this;
    this.dataset = new Proxy({}, {
      get: (_, k) => el.getAttribute(`data-${kebab(String(k))}`) ?? undefined,
      set: (_, k, v) => { el.setAttribute(`data-${kebab(String(k))}`, String(v)); return true; },
      has: (_, k) => el.hasAttribute(`data-${kebab(String(k))}`),
      ownKeys: () => [...el._attrs.keys()].filter((a) => a.startsWith('data-')).map((a) => a.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
    });
    this.classList = {
      add: (...c) => { const s = new Set(el._classes()); c.forEach((x) => s.add(x)); el.className = [...s].join(' '); },
      remove: (...c) => { el.className = el._classes().filter((x) => !c.includes(x)).join(' '); },
      toggle: (c, force) => {
        const on = force === undefined ? !el._classes().includes(c) : Boolean(force);
        if (on) el.classList.add(c);
        else el.classList.remove(c);
        return on;
      },
      contains: (c) => el._classes().includes(c),
    };
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.files = null;
  }

  _classes() { return (this.getAttribute('class') || '').split(/\s+/).filter(Boolean); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', String(v)); }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }
  get title() { return this.getAttribute('title') || ''; }
  set title(v) { this.setAttribute('title', v); }
  get href() { return this.getAttribute('href') || ''; }
  set href(v) { this.setAttribute('href', v); }
  get src() { return this.getAttribute('src') || ''; }
  set src(v) { this.setAttribute('src', v); }
  get type() { return this.getAttribute('type') || (this.tagName === 'INPUT' ? 'text' : ''); }
  set type(v) { this.setAttribute('type', v); }
  get tabIndex() { return Number(this.getAttribute('tabindex') ?? -1); }
  get children() { return this.childNodes.filter((c) => c instanceof Element); }
  get firstElementChild() { return this.children[0] || null; }
  get isContentEditable() { return this.getAttribute('contenteditable') === 'true'; }
  set innerHTML(v) { this._detachAll(); this._html = String(v); }
  get innerHTML() { return this._html || ''; }
  get outerHTML() { return `<${this.localName}>${this.textContent}</${this.localName}>`; }

  setAttribute(k, v) { this._attrs.set(String(k).toLowerCase(), String(v)); }
  getAttribute(k) { return this._attrs.has(String(k).toLowerCase()) ? this._attrs.get(String(k).toLowerCase()) : null; }
  hasAttribute(k) { return this._attrs.has(String(k).toLowerCase()); }
  removeAttribute(k) { this._attrs.delete(String(k).toLowerCase()); }

  focus() {
    const doc = this.ownerDocument;
    if (doc.activeElement === this) return;
    doc.activeElement = this;
    this.dispatchEvent(new Event('focus'));
  }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; }
  click() { this.dispatchEvent(new Event('click', { bubbles: true, detail: 0, button: 0 })); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 600, bottom: 800, width: 600, height: 800, x: 0, y: 0 }; }
  setPointerCapture() {}
  releasePointerCapture() {}
  scrollIntoView() {}
  scrollTo() {}
  showModal() { this.setAttribute('open', ''); }
  close() { this.removeAttribute('open'); this.dispatchEvent(new Event('close')); }
  getContext() { return new Proxy({}, { get: () => () => {} }); }

  matches(sel) { return parseList(sel).some((chain) => matchChain(this, chain)); }
  closest(sel) {
    for (let n = this; n instanceof Element; n = n.parentNode) if (n.matches(sel)) return n;
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (!(c instanceof Element)) continue;
        if (c.matches(sel)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

// ── selectors ────────────────────────────────────────────────────────────────

function parseList(sel) {
  return splitTop(String(sel), ',').map((s) => splitTop(s.trim(), ' ').filter(Boolean).map(parseCompound));
}

function splitTop(s, ch) {
  const out = [];
  let depth = 0;
  let quote = null;
  let cur = '';
  for (const c of s) {
    if (quote) {
      if (c === quote) quote = null;
      cur += c;
      continue;
    }
    if (c === '"' || c === "'") quote = c;
    if (c === '(' || c === '[') depth++;
    if (c === ')' || c === ']') depth--;
    if (c === ch && depth === 0) {
      out.push(cur);
      cur = '';
    } else cur += c;
  }
  out.push(cur);
  return out;
}

function parseCompound(s) {
  const c = { tag: null, id: null, classes: [], attrs: [], not: [], disabled: false };
  const re = /^([a-zA-Z][\w-]*|\*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([*^$]?=)["']?([^"'\]]*)["']?)?\]|:not\(((?:[^()]|\([^()]*\))*)\)|:disabled/g;
  let m;
  while ((m = re.exec(s))) {
    if (m[1]) c.tag = m[1] === '*' ? null : m[1].toLowerCase();
    else if (m[2]) c.id = m[2];
    else if (m[3]) c.classes.push(m[3]);
    else if (m[4]) c.attrs.push({ name: m[4].toLowerCase(), op: m[5] || null, value: m[6] ?? null });
    else if (m[7] != null) c.not.push(...parseList(m[7]));
    else if (m[0] === ':disabled') c.disabled = true;
  }
  return c;
}

function matchCompound(el, c) {
  if (!(el instanceof Element)) return false;
  if (c.tag && el.localName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  const cls = el._classes();
  if (c.classes.some((x) => !cls.includes(x))) return false;
  for (const a of c.attrs) {
    const v = el.getAttribute(a.name);
    if (v == null) return false;
    if (a.op === '=' && v !== a.value) return false;
    if (a.op === '^=' && !v.startsWith(a.value)) return false;
    if (a.op === '$=' && !v.endsWith(a.value)) return false;
    if (a.op === '*=' && !v.includes(a.value)) return false;
  }
  if (c.disabled && !el.disabled) return false;
  if (c.not.some((chain) => matchChain(el, chain))) return false;
  return true;
}

function matchChain(el, chain) {
  if (!matchCompound(el, chain[chain.length - 1])) return false;
  let i = chain.length - 2;
  let n = el.parentNode;
  while (i >= 0 && n) {
    if (matchCompound(n, chain[i])) i--;
    n = n.parentNode;
  }
  return i < 0;
}

// ── document / window ────────────────────────────────────────────────────────

class Document extends Node {
  constructor() {
    super(null);
    this.ownerDocument = this;
    this.documentElement = new Element(this, 'html');
    this._adopt(this.documentElement);
    this.body = new Element(this, 'body');
    this.documentElement.append(this.body);
    this.activeElement = this.body;
  }
  createElement(tag) { return new Element(this, tag); }
  createElementNS(ns, tag) { return new Element(this, tag, ns); }
  createTextNode(t) { return new Text(this, t); }
  querySelector(sel) { return this.documentElement.querySelector(sel); }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
  get isConnected() { return true; }
}

/** Install document/window/Node globals; returns {document, window, uninstall()}. */
export function installDom({ coarse = false } = {}) {
  const saved = {};
  const doc = new Document();
  const win = new Node(doc);
  doc.defaultView = win;
  Object.assign(win, {
    document: doc,
    matchMedia: (q) => ({ matches: coarse && /pointer:\s*coarse/.test(q), addEventListener() {}, removeEventListener() {} }),
    devicePixelRatio: 1,
    innerWidth: 1280,
    innerHeight: 900,
    scrollTo() {},
  });
  const globals = {
    document: doc,
    window: win,
    Node,
    Element,
    HTMLElement: Element,
    matchMedia: win.matchMedia,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    cancelAnimationFrame: (t) => clearTimeout(t),
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  };
  for (const [k, v] of Object.entries(globals)) {
    saved[k] = Object.getOwnPropertyDescriptor(globalThis, k);
    Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  }
  win.addEventListener = Node.prototype.addEventListener.bind(win);
  return {
    document: doc,
    window: win,
    /** Fire a keydown at the focused element (bubbles to window like a real key press). */
    key(key, init = {}) {
      const ev = new Event('keydown', { bubbles: true, key, ...init });
      (doc.activeElement || doc.body).dispatchEvent(ev);
      return ev;
    },
    uninstall() {
      for (const [k, d] of Object.entries(saved)) {
        if (d) Object.defineProperty(globalThis, k, d);
        else delete globalThis[k];
      }
    },
  };
}

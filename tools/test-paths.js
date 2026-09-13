/**
 * test-paths.js — 验证两种部署形态的加载路径
 *
 * ① file:// 双击：内联题库生效，不发 fetch
 * ② http:// 部署：内联为空时 fetch ./bank.json 成功
 * ③ 都没有：给出友好错误提示（不白屏）
 *
 * 用法：node tools/test-paths.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const srcHtml = fs.readFileSync(path.join(ROOT, '3-测验', 'index.html'), 'utf8');
const bank = JSON.parse(fs.readFileSync(path.join(ROOT, '3-测验', 'bank.json'), 'utf8'));

const L = [];

function makeEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', checked: false,
    dataset: {}, style: {},
    classList: { _s: new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
                 toggle(c,f){ f===undefined ? (this._s.has(c)?this._s.delete(c):this._s.add(c)) : (f?this._s.add(c):this._s.delete(c)); },
                 contains(c){ return this._s.has(c); } },
    scrollIntoView(){}, appendChild(){}, querySelectorAll(){ return []; },
    get innerHTML(){ return this._html; }, set innerHTML(v){ this._html = String(v); },
  };
  Object.defineProperty(el,'onclick',{set(v){el._onclick=v},get(){return el._onclick},configurable:true});
  Object.defineProperty(el,'onchange',{set(v){el._onchange=v},get(){return el._onchange},configurable:true});
  return el;
}

function run(htmlContent, fetchImpl, label, expect) {
  const code = [...htmlContent.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
  const els = {}, store = {};
  const errs = [];
  const sandbox = {
    console: { log(){}, info(){}, warn(){}, error(...a){ errs.push(a.join(' ')); } },
    Math, JSON, Date, Object, Array, String, Number, Boolean, Promise,
    parseInt, parseFloat, isNaN, RegExp, Error, Set, Map,
    document: {
      getElementById(id){ return els[id] || (els[id] = makeEl(id)); },
      querySelectorAll(){ return []; }, addEventListener(){}, createElement(){ return makeEl('_'); },
    },
    window: { innerWidth: 1200, scrollTo(){}, addEventListener(){} },
    localStorage: { getItem(k){ return k in store ? store[k] : null; },
                    setItem(k,v){ store[k] = String(v); }, removeItem(k){ delete store[k]; } },
    fetch: fetchImpl,
    confirm(){ return true; }, alert(){}, setTimeout(fn){ return 0; }, clearTimeout(){},
  };
  sandbox.globalThis = sandbox;
  vm.runInContext(code, vm.createContext(sandbox), { filename: label });
  return new Promise(r => setTimeout(() => r({ els, errs }), 120)).then(({ els, errs }) => {
    const sheet = els['sheet'] ? els['sheet'].innerHTML : '';
    const cards = (sheet.match(/class="qcard"/g) || []).length;
    const loadFail = sheet.indexOf('题库加载失败') >= 0;
    const got = { cards, loadFail };
    const pass = expect(got);
    L.push((pass ? '  ✅ ' : '  ❌ ') + label + ' → 题卡 ' + cards + (loadFail ? ' / 走失败分支' : ''));
    if (!pass) L.push('       详: ' + JSON.stringify(got) + (errs.length ? ' errs=' + errs.join('|') : ''));
  });
}

(async () => {
  L.push('=== 两种部署形态 ===');

  /* ① 内联形态：fetch 应完全不被调用 */
  let fetchCalled = false;
  await run(srcHtml, () => { fetchCalled = true; return Promise.reject(new Error('不应调用 fetch')); },
    'file:// 双击（内联题库）',
    g => g.cards === 10 && !g.loadFail);
  L.push(fetchCalled ? '  ❌ 内联形态竟然调用了 fetch' : '  ✅ 内联形态未调用 fetch');

  /* ② 外置形态：把内联清空，模拟部署版本 */
  const externalHtml = srcHtml.replace(
    /const BANK_INLINE = \/\*__BANK_INLINE__\*\/[\s\S]*?\/\*__END_BANK_INLINE__\*\//,
    'const BANK_INLINE = /*__BANK_INLINE__*/[]/*__END_BANK_INLINE__*/');
  let fetchedUrl = null;
  await run(externalHtml, (url) => {
    fetchedUrl = url;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(bank) });
  }, 'http:// 部署（外置 bank.json）',
    g => g.cards === 10 && !g.loadFail);
  L.push(fetchedUrl === './bank.json' ? '  ✅ 外置形态 fetch 了 ./bank.json'
                                      : '  ❌ fetch 地址异常: ' + fetchedUrl);

  /* ③ 都没有：友好报错 */
  await run(externalHtml, () => Promise.reject(new Error('Failed to fetch')),
    '两者皆无（无网络/未构建）',
    g => g.loadFail && g.cards === 0);
  L.push('  （此项应显示「走失败分支」，代表给出了友好提示而非白屏）');

  const out = L.join('\n');
  fs.writeFileSync(path.join(__dirname, '_paths.txt'), out, 'utf8');
  const fails = (out.match(/❌/g) || []).length;
  console.log(fails ? ('路径验证有 ' + fails + ' 项失败') : '路径验证全部通过');
})();

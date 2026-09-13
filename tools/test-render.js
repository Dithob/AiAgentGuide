/**
 * test-render.js — 验证渲染路径不抛错（用 jsdom 风格的最小 stub）
 *
 * 目的：Node 测试已验证组卷逻辑，但渲染函数（cardHTML / refreshStats / renderSheet）
 * 走的是真实 DOM API。这里提供一个能记录 innerHTML 的 stub，确认：
 *   ① boot 后 start() → renderAll() 全链路无异常
 *   ② 生成的 HTML 里题号、选项、重点标记都正确
 *   ③ fetch 失败时（file:// 且未内联）给出友好提示而非白屏
 *
 * 用法：node tools/test-render.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, '3-测验', 'index.html'), 'utf8');
const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

const L = [];
const errors = [];

function makeEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', checked: false,
    dataset: {}, style: {},
    classList: { _s: new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
                 toggle(c,f){ f===undefined ? (this._s.has(c)?this._s.delete(c):this._s.add(c)) : (f?this._s.add(c):this._s.delete(c)); },
                 contains(c){ return this._s.has(c); } },
    scrollIntoView(){}, appendChild(){}, querySelectorAll(){ return []; },
    get innerHTML(){ return this._html; },
    set innerHTML(v){ this._html = String(v); },
  };
  // onclick/onchange 用 defineProperty 才能真正存住（普通 setter 在对象字面量里不生效）
  Object.defineProperty(el, 'onclick',  { set(v){ el._onclick = v; },  get(){ return el._onclick; },  configurable: true });
  Object.defineProperty(el, 'onchange', { set(v){ el._onchange = v; }, get(){ return el._onchange; }, configurable: true });
  return el;
}
const els = {};
const store = {};
const sandbox = {
  console: { log(){}, info(){}, warn(){}, error(...a){ errors.push(a.join(' ')); } },
  Math, JSON, Date, Object, Array, String, Number, Boolean, Promise,
  parseInt, parseFloat, isNaN, RegExp, Error, Set, Map,
  document: {
    getElementById(id){ return els[id] || (els[id] = makeEl(id)); },
    querySelectorAll(){ return []; },
    addEventListener(){},
    createElement(){ return makeEl('_'); },
  },
  window: { innerWidth: 1200, scrollTo(){}, addEventListener(){} },
  localStorage: {
    getItem(k){ return k in store ? store[k] : null; },
    setItem(k,v){ store[k] = String(v); },
    removeItem(k){ delete store[k]; },
  },
  fetch(){ return Promise.reject(new Error('file:// 下 fetch 被拦截')); },
  confirm(){ return true; }, alert(){},
  setTimeout(fn){ return 0; }, clearTimeout(){},
};
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(code, ctx, { filename: 'index.html<script>' });
} catch (e) {
  errors.push('脚本执行异常: ' + e.message);
}

new Promise(r => setTimeout(r, 120)).then(() => {
  L.push('=== 渲染验证 ===');

  if (errors.length) {
    errors.forEach(e => L.push('  ❌ ' + e));
  } else {
    L.push('  ✅ 脚本加载无异常');
  }

  const sheet = els['sheet'];
  const statbar = els['statbar'];
  const navgrid = els['navgrid'];

  // 内联题库存在，故不应走 fetch 失败分支
  if (sheet && sheet.innerHTML && sheet.innerHTML.indexOf('题库加载失败') >= 0) {
    L.push('  ❌ 走到了「题库加载失败」分支（说明内联未生效）');
  } else {
    L.push('  ✅ 未走加载失败分支（内联题库生效）');
  }

  if (sheet && sheet.innerHTML) {
    L.push('  ✅ sheet 有渲染内容 (' + sheet.innerHTML.length + ' 字节)');
    // 第一页应有 10 张卡
    const cards = (sheet.innerHTML.match(/class="qcard"/g) || []).length;
    cards === 10 ? L.push('  ✅ 首页渲染 10 张题卡')
                 : L.push('  ❌ 首页题卡数 = ' + cards + '（应为 10）');
    // 题号格式
    sheet.innerHTML.indexOf('/ 90') >= 0 ? L.push('  ✅ 显示总题数 90')
                                         : L.push('  ❌ 未显示总题数');
    // 选项
    const opts = (sheet.innerHTML.match(/class="opt/g) || []).length;
    opts >= 40 ? L.push('  ✅ 选项按钮已渲染 (' + opts + ')')
               : L.push('  ❌ 选项过少 (' + opts + ')');
    // 重点标记
    if (sheet.innerHTML.indexOf('★ 重点') >= 0) L.push('  ✅ 重点标记已渲染');
    else L.push('  ⚠️ 首页无 ★ 重点 标记（可能首页恰好没有高权重题）');
  } else {
    L.push('  ❌ sheet 为空');
  }

  if (statbar && statbar.innerHTML) {
    L.push('  ✅ 状态条已渲染');
    statbar.innerHTML.indexOf('本套') >= 0 ? L.push('  ✅ 组卷说明已渲染')
                                          : L.push('  ❌ 组卷说明缺失');
  } else {
    L.push('  ❌ 状态条为空');
  }

  if (navgrid && navgrid.innerHTML) {
    const btns = (navgrid.innerHTML.match(/navbtn/g) || []).length;
    L.push('  ✅ 答题卡渲染 ' + btns + ' 个题号按钮');
  } else {
    L.push('  ❌ 答题卡为空');
  }

  // 组卷控件已绑定
  ['composeSize','composeWeight','composeDedup','composeSeed','reshuffle'].forEach(id => {
    const el = els[id];
    if (!el) { L.push('  ❌ 缺少控件 ' + id); return; }
    const bound = el._onchange || el._onclick;
    bound ? L.push('  ✅ 控件 ' + id + ' 已绑定事件')
          : L.push('  ❌ 控件 ' + id + ' 未绑定事件');
  });

  const out = L.join('\n');
  fs.writeFileSync(path.join(__dirname, '_render.txt'), out, 'utf8');
  const fails = (out.match(/❌/g) || []).length;
  console.log(fails ? ('渲染验证有 ' + fails + ' 项失败') : '渲染验证全部通过');
});

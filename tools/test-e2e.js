/**
 * test-e2e.js — 端到端「真实答题流程」验证
 *
 * 区别于 test-engine / test-render（只测单点函数），本文件模拟一个真实用户
 * 从头到尾做完一套卷子，验证交互闭环是否真的成立：
 *   ① 打开页面 → 自动组卷出题
 *   ② 点击选项 → 答案被记录（不能点了没反应）
 *   ③ 交卷判分 → 正确数计算正确
 *   ④ 错题进错题池，且存的是稳定 id（不是数组下标）
 *   ⑤ 错题池能跨「重新组卷」保留 —— 这是本次重构的核心目的
 *   ⑥ 换一套后题序变化，错题池仍指向原来的题（不串号）
 *   ⑦ 错题重做模式能捞回之前做错的题
 *
 * 依赖 index.html 尾部的 globalThis.__t 测试钩子。
 * 用法：node tools/test-e2e.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, '3-测验', 'index.html'), 'utf8');
const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

const L = [];
let pass = 0, fail = 0;
function ok(cond, msg) { if (cond) { pass++; L.push('  ✅ ' + msg); } else { fail++; L.push('  ❌ ' + msg); } }
function warn(msg) { L.push('  ⚠️ ' + msg); }

// ---------- DOM 桩件 ----------
const els = {};
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
  for (const ev of ['onclick', 'onchange', 'oninput']) {
    Object.defineProperty(el, ev, { set(v){ el['_' + ev] = v; }, get(){ return el['_' + ev]; }, configurable: true });
  }
  el.addEventListener = () => {};
  return el;
}

const store = {};
const errors = [];
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
    setItem(k, v){ store[k] = String(v); },
    removeItem(k){ delete store[k]; },
  },
  fetch(){ return Promise.reject(new Error('file:// 下 fetch 被拦截')); },
  confirm(){ return true; }, alert(){},
  setTimeout(fn, ms){ return global.setTimeout(fn, 0); }, clearTimeout(){},
};
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(code, ctx, { filename: 'index.html<script>' });
} catch (e) {
  errors.push('脚本执行异常: ' + e.message);
}

const WRONG_KEY = 'aag_quiz_wrong_v3';
const picks = () => (sandbox.__t ? sandbox.__t.state().picks : {}) || {};
const getWrong = () => { try { return JSON.parse(store[WRONG_KEY] || '[]'); } catch (e) { return []; } };

setTimeout(() => {
  L.push('════════ 端到端答题流程验证 ════════');

  if (errors.length) errors.forEach(e => L.push('  ❌ 运行期异常: ' + e));
  else L.push('  ✅ 脚本加载无异常');

  const T = sandbox.__t;
  if (!T) {
    L.push('  ❌ 测试钩子 globalThis.__t 未挂载');
    finish();
    return;
  }
  ok(typeof T.start === 'function', '测试钩子可用');

  const bank = T.bank();
  const bankIds = new Set(bank.map(x => x.id));

  // ---------- ① 组卷 ----------
  L.push('');
  L.push('【① 打开页面自动组卷】');
  T.start();
  let qs = T.queue();
  ok(qs.length > 0, '组卷得到 ' + qs.length + ' 题');
  ok(qs.every(x => x.q && x.q.id), '每题都有稳定 id');
  // 去重仅在限量组卷时生效（不限量时去重会丢题，这是设计选择）
  T.setParam('size', 20); T.start();
  const q20 = T.queue();
  const c20 = q20.map(x => x.q.concept);
  ok(q20.length === 20, '限量组卷得到 ' + q20.length + ' 题（size=20）');
  ok(new Set(c20).size === c20.length,
     '限量时同 concept 不重复：' + new Set(c20).size + '/' + c20.length);
  T.setParam('size', 0); T.start();
  L.push('     本套题型：' + qs.map(x => x.q.t).filter((v, i, a) => a.indexOf(v) === i).join('+'));

  // ---------- ② 答题 ----------
  L.push('');
  L.push('【② 点击选项是否被记录】');
  const q0 = qs[0].q;
  const a0 = Array.isArray(q0.a) ? q0.a[0] : q0.a;
  T.pick(0, a0);
  ok(String((picks()[0]||[])[0]) === String(a0), '单选：选 ' + a0 + ' → 记为 ' + JSON.stringify(picks()[0]));

  const mi = qs.findIndex(x => x.q.t === 'multi');
  if (mi >= 0) {
    const mq = qs[mi].q;
    const mAns = Array.isArray(mq.a) ? mq.a : [mq.a];
    // 多选是 toggle 语义；先把该位置的答案重置成数组，避免与单选题残留的标量冲突
    picks()[mi] = [];
    mAns.forEach(x => T.pick(mi, x));
    ok(Array.isArray(picks()[mi]) && mAns.every(x => picks()[mi].indexOf(x) >= 0),
       '多选：选 ' + mAns.join('') + ' → 记为 [' + (picks()[mi]||[]).join(',') + ']');
    // 验证 toggle：再点一次同一项应被取消，再补回
    T.pick(mi, mAns[0]);
    const afterToggle = (picks()[mi]||[]).slice();
    ok(afterToggle.indexOf(mAns[0]) < 0,
       '多选 toggle：再点 ' + mAns[0] + ' 后被取消 → [' + afterToggle.join(',') + ']');
    T.pick(mi, mAns[0]);   // 补回，保持答案完整
    // mi===0 时会把第 ② 步的单选答案覆盖成多选，故第 0 题改选其正确答案
    if (mi === 0) { T.pick(0, a0); }
  } else { warn('本套无多选题，跳过'); }

  // ---------- ③ 判分 ----------
  L.push('');
  L.push('【③ 交卷判分】');
  // 故意把第 1 题答错
  let wrongId = null;
  if (qs[1]) {
    const q1 = qs[1].q;
    const right = Array.isArray(q1.a) ? q1.a : [q1.a];
    const letters = (Array.isArray(q1.o) ? q1.o : String(q1.o).split('\n')).map((_, i) => String.fromCharCode(65 + i));
    const wrongPick = letters.find(x => right.indexOf(x) < 0);
    if (wrongPick) { T.pick(1, wrongPick); wrongId = q1.id; }
  }
  const P = picks();
  const answeredBefore = Object.keys(P).filter(k => P[k] && P[k].length).length;
  T.gradeAll();
  const st = T.state();
  ok(typeof T.rightCount() === 'number', '判分完成，正确数 = ' + T.rightCount() + ' / 已判 ' + T.gradedCount());
  ok(answeredBefore >= 2, '交卷时已答 ' + answeredBefore + ' 题');
  ok(T.rightCount() <= answeredBefore, '正确数不超过已答数（' + T.rightCount() + ' ≤ ' + answeredBefore + '）');

  // ---------- ④ 错题池 ----------
  L.push('');
  L.push('【④ 错题池存的是稳定 id】');
  const w = getWrong();
  ok(Array.isArray(w), '错题池已写入（' + w.length + ' 条）');
  if (w.length) {
    ok(typeof w[0] === 'string', '元素是字符串 id（如 ' + w[0] + '），不是数组下标');
    ok(w.every(x => bankIds.has(x)), '每个 id 都能在题库中找到');
    if (wrongId) ok(w.indexOf(wrongId) >= 0, '故意答错的 ' + wrongId + ' 确实进了错题池');
  } else if (wrongId) {
    fail++; L.push('  ❌ 答错了 ' + wrongId + ' 却没进错题池');
  }

  // ---------- ⑤ 重新组卷后错题池保留 ----------
  L.push('');
  L.push('【⑤ 重新组卷后错题池仍保留】');
  const wBefore = getWrong().slice();
  T.newSeed();
  T.start();
  const wAfter = getWrong();
  ok(JSON.stringify(wAfter) === JSON.stringify(wBefore),
     '重新组卷后错题池不变（' + wAfter.length + ' 条）');
  ok(wAfter.every(x => bankIds.has(x)), '重新组卷后 id 仍全部可解析');

  // ---------- ⑥ 换一套题序变化但错题不串号 ----------
  L.push('');
  L.push('【⑥ 换一套后题序变化，错题不串号】');
  const qA = T.queue().map(x => x.q.id);
  let changed = false;
  for (let k = 0; k < 12 && !changed; k++) {
    T.newSeed(); T.start();
    const qB = T.queue().map(x => x.q.id);
    if (JSON.stringify(qA) !== JSON.stringify(qB)) changed = true;
  }
  ok(changed, '多次换种子后题序/题集确实变化（新卷子）');
  ok(JSON.stringify(getWrong()) === JSON.stringify(wBefore),
     '换卷后错题池依旧不变 → 不会指错题');

  // ---------- ⑦ 错题重做 ----------
  L.push('');
  L.push('【⑦ 错题重做模式能捞回错题】');
  const idsInPool = getWrong();
  if (idsInPool.length) {
    T.setMode('wrong');
    const wq = T.queue();
    ok(wq.length === idsInPool.length,
       '错题重做捞回 ' + wq.length + ' 题（池中 ' + idsInPool.length + '）');
    ok(wq.every(x => idsInPool.indexOf(x.q.id) >= 0), '捞回的题确实都是错题池里的');
  } else { warn('错题池为空，跳过'); }

  // ---------- ⑧ 答对后从错题池移除 ----------
  L.push('');
  L.push('【⑧ 错题做对了会从池中移除】');
  if (idsInPool.length) {
    const first = idsInPool[0];
    T.setMode('wrong');
    const pos = T.queue().findIndex(x => x.q.id === first);
    if (pos >= 0) {
      const qq = T.queue()[pos].q;
      const ra = Array.isArray(qq.a) ? qq.a : [qq.a];
      ra.forEach(x => T.pick(pos, x));
      T.gradeAll();
      ok(getWrong().indexOf(first) < 0, '答对后 ' + first + ' 已移出错题池');
    } else { warn('未在当前卷中找到 ' + first); }
  } else { warn('错题池为空，跳过'); }

  finish();
}, 150);

function finish() {
  L.push('');
  L.push('─────────────────────────────');
  L.push((fail ? '❌ ' : '✅ ') + '端到端：' + pass + ' 通过 / ' + fail + ' 失败');
  const out = L.join('\n');
  fs.writeFileSync(path.join(__dirname, '_e2e.txt'), out, 'utf8');
  console.log(out);
  process.exit(fail ? 1 : 0);
}

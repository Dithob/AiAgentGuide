/**
 * test-engine.js — 在 Node 里跑通 index.html 的组卷引擎（无浏览器）
 *
 * 做法：把 index.html 里的 <script> 抽出来，注入最小 DOM/localStorage stub 后执行，
 * 然后直接调用 buildQueue() 验证各种参数组合下的组卷行为。
 *
 * 用法：node tools/test-engine.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, '3-测验', 'index.html'), 'utf8');

/* 抽出 <script> 内容 */
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) throw new Error('未找到 script');
const code = scripts.join('\n');

/* ---------- 最小 DOM stub ---------- */
function makeEl(id) {
  return {
    id,
    innerHTML: '', textContent: '', value: '', checked: false,
    dataset: {}, style: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    set onclick(v){ this._onclick = v; }, get onclick(){ return this._onclick; },
    set onchange(v){ this._onchange = v; }, get onchange(){ return this._onchange; },
    scrollIntoView(){}, appendChild(){}, querySelectorAll(){ return []; },
  };
}
const els = {};
const localStorageData = {};
const sandbox = {
  console,
  Math, JSON, Date, Object, Array, String, Number, Boolean, Promise, parseInt, parseFloat, isNaN,
  document: {
    getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement() { return makeEl('_'); },
  },
  window: { innerWidth: 1200, scrollTo() {}, addEventListener() {} },
  localStorage: {
    getItem(k) { return k in localStorageData ? localStorageData[k] : null; },
    setItem(k, v) { localStorageData[k] = String(v); },
    removeItem(k) { delete localStorageData[k]; },
  },
  fetch() { return Promise.reject(new Error('no network in test')); },
  confirm() { return true; },
  alert() {},
  setTimeout(fn) { return 0; },
  clearTimeout() {},
};
sandbox.globalThis = sandbox;

/* ---------- 执行 ---------- */
// boot 被写成 IIFE，外部拿不到。这里在源码尾部补一句导出，
// 把测试需要的东西挂到 sandbox 上。
const exportTail = `
;globalThis.__t = { get BANK(){return BANK}, state, buildQueue, saveWrong, loadWrong, addWrong, delWrong, migrateWrongPool, BANK_OLD_ORDER_IDS };
`;
const ctx = vm.createContext(sandbox);
vm.runInContext(code + exportTail, ctx, { filename: 'index.html<script>' });

/* boot 是 async IIFE，已在 runInContext 时启动；等它把 BANK 填好 */
new Promise(r => setTimeout(r, 100)).then(() => runTests());

function runTests() {
  const L = [];
  const ok = (s) => L.push('  ✅ ' + s);
  const bad = (s) => L.push('  ❌ ' + s);

  const T = sandbox.__t;
  const BANK = T.BANK;
  const state = T.state;
  const buildQueue = T.buildQueue;

  L.push('=== 基础 ===');
  if (!BANK || !BANK.length) { bad('BANK 未加载'); return finish(L); }
  ok('题库加载: ' + BANK.length + ' 题');
  if (typeof buildQueue !== 'function') { bad('buildQueue 不是函数'); return finish(L); }

  const ids = BANK.map(q => q.id);
  const dupId = ids.filter((x, i) => ids.indexOf(x) !== i);
  dupId.length ? bad('id 重复: ' + dupId.join(',')) : ok('id 全部唯一 (' + ids.length + ')');

  const noConcept = BANK.filter(q => !q.concept);
  noConcept.length ? bad('缺 concept: ' + noConcept.length + ' 题') : ok('concept 字段齐全');

  const noW = BANK.filter(q => !(q.w >= 1 && q.w <= 5));
  noW.length ? bad('w 越界: ' + noW.length + ' 题') : ok('w 全部在 1~5');

  /* ---------- 组卷行为 ---------- */
  L.push('');
  L.push('=== 组卷：模式筛选 ===');
  const reset = (o) => Object.assign(state, {
    mode: 'all', chapter: 'all', size: 0, weight: 'weighted',
    dedup: true, seed: true, seedVal: 12345, page: 0,
  }, o || {});

  reset(); let q = buildQueue();
  q.length === 90 ? ok('全部练习 → 90 题') : bad('全部练习 → ' + q.length + ' 题（应为 90）');

  reset({ mode: 'star' }); q = buildQueue();
  q.length === 18 ? ok('🔆强化专项 → 18 题') : bad('🔆强化专项 → ' + q.length + ' 题（应为 18）');

  reset({ chapter: 'ch01' }); q = buildQueue();
  q.length === 27 ? ok('仅 ch01 → 27 题') : bad('仅 ch01 → ' + q.length + ' 题（应为 27）');

  reset({ chapter: 'star' }); q = buildQueue();
  q.length === 18 ? ok('仅强化（章节）→ 18 题') : bad('仅强化 → ' + q.length + ' 题');

  /* ---------- 规模 ---------- */
  L.push('');
  L.push('=== 组卷：规模 ===');
  [10, 20, 30].forEach(n => {
    reset({ size: n }); const r = buildQueue();
    r.length === n ? ok('每套 ' + n + ' 题 → ' + r.length) : bad('每套 ' + n + ' → ' + r.length);
    const uniq = new Set(r.map(x => x.q.id)).size;
    uniq === r.length ? ok('  无重复题') : bad('  有重复题: ' + r.length + ' vs 唯一 ' + uniq);
  });

  /* ---------- 同知识点去重 ---------- */
  L.push('');
  L.push('=== 组卷：同知识点去重 ===');
  reset({ size: 30, dedup: true });
  for (let trial = 0; trial < 30; trial++) {
    state.seedVal = trial * 7919;
    const r = buildQueue();
    const cs = r.map(x => x.q.concept);
    const dup = cs.filter((c, i) => cs.indexOf(c) !== i);
    if (dup.length) { bad('第 ' + trial + ' 次出现同 concept 重复: ' + dup.join(',')); break; }
    if (trial === 29) ok('30 次抽样均无同 concept 重复（每套 30 题）');
  }

  /* dedup 关闭时应允许重复 */
  reset({ size: 30, dedup: false });
  let seenDup = false;
  for (let trial = 0; trial < 40; trial++) {
    state.seedVal = trial * 104729;
    const cs = buildQueue().map(x => x.q.concept);
    if (cs.some((c, i) => cs.indexOf(c) !== i)) { seenDup = true; break; }
  }
  seenDup ? ok('关闭去重后允许同 concept 出现（符合预期）') : bad('关闭去重后仍未出现重复，可能去重没生效');

  /* ---------- 权重 ---------- */
  L.push('');
  L.push('=== 组卷：权重倾斜 ===');
  reset({ size: 30, weight: 'top' }); q = buildQueue();
  const allTop = q.every(x => x.q.w >= 4);
  allTop ? ok('只取重点题(w≥4) → ' + q.length + ' 题，全部达标') : bad('top 模式含 w<4 的题');

  /* weighted 应比 flat 更容易抽到高权重题：比较平均权重 */
  function avgW(mode) {
    reset({ size: 20, weight: mode, dedup: false });
    let sum = 0, n = 0;
    for (let t = 0; t < 200; t++) {
      state.seedVal = t * 31337;
      const r = buildQueue();
      r.forEach(x => { sum += x.q.w; n++; });
    }
    return sum / n;
  }
  const aw = avgW('weighted'), af = avgW('flat');
  L.push('  weighted 平均权重=' + aw.toFixed(3) + '　flat 平均权重=' + af.toFixed(3));
  aw > af ? ok('按权重抽题确实偏向重点（' + (aw - af).toFixed(3) + ' 差值）')
          : bad('weighted 未比 flat 更偏向重点');

  /* ---------- 种子随机可复现 ---------- */
  L.push('');
  L.push('=== 组卷：随机可复现 ===');
  reset({ size: 20, seed: true, seedVal: 999 });
  const r1 = buildQueue().map(x => x.q.id).join(',');
  reset({ size: 20, seed: true, seedVal: 999 });
  const r2 = buildQueue().map(x => x.q.id).join(',');
  r1 === r2 ? ok('同种子 → 同题卷（可复现）') : bad('同种子结果不同');

  reset({ size: 20, seed: true, seedVal: 1000 });
  const r3 = buildQueue().map(x => x.q.id).join(',');
  r3 !== r1 ? ok('换种子 → 换题卷') : bad('换种子题卷未变');

  /* ---------- 错题池按 id ---------- */
  L.push('');
  L.push('=== 错题池（按 id）===');
  T.saveWrong([]);
  T.addWrong('ch01-07');
  T.addWrong('star-14');
  T.addWrong('ch01-07'); /* 重复添加应幂等 */
  const w = T.loadWrong();
  (w.length === 2 && w.indexOf('ch01-07') >= 0 && w.indexOf('star-14') >= 0)
    ? ok('添加幂等，当前 ' + w.length + ' 条') : bad('错题池内容异常: ' + JSON.stringify(w));

  reset({ mode: 'wrong' });
  q = buildQueue();
  const wrongIds = q.map(x => x.q.id).sort().join(',');
  wrongIds === 'ch01-07,star-14' ? ok('错题重做模式只含错题池的题') : bad('错题重做 → ' + wrongIds);

  /* ---------- 迁移 ---------- */
  L.push('');
  L.push('=== 旧错题池迁移 ===');
  // 模拟旧版存了下标 [0, 5]（对应旧顺序前几题）
  T.saveWrong([]);
  localStorageData['aag_quiz_wrong_v2'] = JSON.stringify([0, 5]);
  const n = T.migrateWrongPool(T.BANK_OLD_ORDER_IDS);
  const migrated = T.loadWrong();
  (n === 2 && migrated.length === 2)
    ? ok('迁移 ' + n + ' 条 → ' + migrated.join(',')) : bad('迁移异常: n=' + n + ' got=' + JSON.stringify(migrated));
  // 迁移后应删除旧键
  !localStorageData['aag_quiz_wrong_v2'] ? ok('旧键已清除') : bad('旧键未清除');
  // 迁移出的 id 应真实存在于题库
  const valid = migrated.every(id => T.BANK.some(x => x.id === id));
  valid ? ok('迁移出的 id 都存在于题库') : bad('迁移出无效 id: ' + migrated.join(','));

  finish(L);
}

function finish(L) {
  const out = L.join('\n');
  fs.writeFileSync(path.join(__dirname, '_test.txt'), out, 'utf8');
  const fails = (out.match(/❌/g) || []).length;
  console.log(fails ? ('测试完成，有 ' + fails + ' 项失败') : '测试完成，全部通过');
}

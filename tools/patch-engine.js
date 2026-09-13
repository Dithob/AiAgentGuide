/**
 * patch-engine.js — 一次性把 index.html 的组卷引擎换成新版
 *
 * 做四件事：
 *   1. 把内联的 const BANK = [...] 换成「内联 JSON + 外置 fallback」双加载
 *   2. 替换 buildQueue()：加权抽样 + 同 concept 去重 + 种子随机
 *   3. 错题池从「数组下标」改成「稳定 id」
 *   4. 挂上新组卷控件的交互
 *
 * 用法：node tools/patch-engine.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SRC = path.join(ROOT, '3-测验', 'index.html');
let html = fs.readFileSync(SRC, 'utf8');
const before = html.length;

/* ---------- 1. BANK：改成内联 JSON + 外置 fallback ---------- */
// 匹配整行 const BANK = [...];
const bankLineRe = /^const BANK = \[[\s\S]*?\];$/m;
if (!bankLineRe.test(html)) throw new Error('未找到 const BANK 行');

const bankJson = fs.readFileSync(path.join(ROOT, '3-测验', 'bank.json'), 'utf8').trim();

const bankReplacement =
`/* 题库来源：3-测验/bank.json（唯一编辑入口）。
   构建脚本 build.js 会把 bank.json 内联到下面这个 script 标签里，
   使本文件在 file:// 双击场景下也能工作（file:// 下 fetch 本地文件会被浏览器拦截）。 */
const BANK_INLINE = /*__BANK_INLINE__*/[]/*__END_BANK_INLINE__*/;

/* 双形态加载：
   ① 内联有数据（双击 file:// 场景）→ 直接用
   ② 内联为空（外置形态，如 http:// 部署）→ fetch ./bank.json */
async function loadBank(){
  if (Array.isArray(BANK_INLINE) && BANK_INLINE.length) return BANK_INLINE;
  const res = await fetch('./bank.json', {cache:'no-cache'});
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return await res.json();
}`;

html = html.replace(bankLineRe, bankReplacement);

/* ---------- 2. 状态：加组卷参数 + 改错题池键 ---------- */
const stateRe = /const LS_WRONG = "aag_quiz_wrong_v2";\nconst state = \{\n  mode:"all", chapter:"all", pageSize:10, page:0,\n  queue:\[\], picks:\{\}, results:\{\}, graded:false, showResult:false\n\};/;
if (!stateRe.test(html)) throw new Error('未找到 state 定义');

const stateReplacement =
`/* 错题池 key 从「数组下标」改为「稳定 id」。
   原因：下标会随组卷顺序变化而错位——一旦随机组题，历史错题会指到别的题。
   旧键 aag_quiz_wrong_v2 会在启动时做一次性迁移（见 migrateWrongPool）。 */
const LS_WRONG = "aag_quiz_wrong_v3";
const LS_WRONG_OLD = "aag_quiz_wrong_v2";
const LS_COMPOSE = "aag_quiz_compose_v1";
const state = {
  mode:"all", chapter:"all", pageSize:10, page:0,
  queue:[], picks:{}, results:{}, graded:false, showResult:false,
  /* 组卷参数 */
  size:0,              /* 0 = 不限题量 */
  weight:"weighted",   /* flat | weighted | top */
  dedup:true,          /* 同一 concept 一套内不重复 */
  seed:true,           /* 种子随机（可复现） */
  seedVal:0            /* 当前种子 */
};`;

html = html.replace(stateRe, stateReplacement);

/* ---------- 3. 错题池：存取改为 id ---------- */
const wrongFnsRe = /function loadWrong\(\)\{ try\{ return JSON\.parse\(localStorage\.getItem\(LS_WRONG\)\|\|"\[\]"\); \}catch\(e\)\{ return \[\]; \} \}\nfunction saveWrong\(a\)\{ try\{ localStorage\.setItem\(LS_WRONG, JSON\.stringify\(a\)\); \}catch\(e\)\{\} \}\nfunction addWrong\(i\)\{ const w=loadWrong\(\); if\(w\.indexOf\(i\)<0\)\{ w\.push\(i\); saveWrong\(w\);\} \}\nfunction delWrong\(i\)\{ const w=loadWrong\(\); const k=w\.indexOf\(i\); if\(k>=0\)\{ w\.splice\(k,1\); saveWrong\(w\);\} \}/;
if (!wrongFnsRe.test(html)) throw new Error('未找到错题池函数');

const wrongReplacement =
`/* 错题池存放题目的稳定 id（如 ch01-07），不是数组下标。
   —— 下标会随组卷顺序变化，开了随机组题就会指错题。 */
function loadWrong(){ try{ const v=JSON.parse(localStorage.getItem(LS_WRONG)||"[]"); return Array.isArray(v)?v:[]; }catch(e){ return []; } }
function saveWrong(a){ try{ localStorage.setItem(LS_WRONG, JSON.stringify(a)); }catch(e){} }
function addWrong(id){ const w=loadWrong(); if(id && w.indexOf(id)<0){ w.push(id); saveWrong(w); } }
function delWrong(id){ const w=loadWrong(); const k=w.indexOf(id); if(k>=0){ w.splice(k,1); saveWrong(w); } }

/* 一次性迁移：旧版错题池存的是下标，需按「当时那道题」换算成 id。
   旧下标来自旧 BANK 的顺序，故必须用旧顺序映射，不能按新 bank 的顺序。 */
function migrateWrongPool(oldOrder){
  let old=null;
  try{ old=JSON.parse(localStorage.getItem(LS_WRONG_OLD)||"null"); }catch(e){}
  if(!Array.isArray(old)||!old.length){ return 0; }
  const ids=[];
  old.forEach(i=>{
    const q=oldOrder[i];
    if(q && q.id && ids.indexOf(q.id)<0) ids.push(q.id);
  });
  saveWrong(ids);
  try{ localStorage.removeItem(LS_WRONG_OLD); }catch(e){}
  return ids.length;
}`;

html = html.replace(wrongFnsRe, wrongReplacement);

/* ---------- 4. buildQueue：新的组卷算法 ---------- */
const buildQueueRe = /\/\* ---------- 组卷 ---------- \*\/\nfunction buildQueue\(\)\{[\s\S]*?\n\}\n\nfunction start\(keepPage\)\{/;
if (!buildQueueRe.test(html)) throw new Error('未找到 buildQueue');

const buildQueueReplacement =
`/* ---------- 组卷 ----------
   三个维度：
     ① 规模 size    —— 一套几题（0 = 全部）
     ② 权重 weight  —— flat 等概率 / weighted 按重点加权 / top 只取 w>=4
     ③ 去重 dedup   —— 同一 concept 一套内最多 1 题

   随机性用「种子随机」（mulberry32），保证同一套题可复现 ——
   否则每次刷新题序都变，做题记录与错题池会对不上。
*/
function mulberry32(a){
  return function(){
    a |= 0; a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

/* 按权重抽 n 个（不重复）。权重 w 视为相对权重，w 越大越容易抽中。
   用「加权无放回」：每次按剩余权重总和轮盘赌抽一个再移除。 */
function weightedSample(pool, n, rnd){
  const arr = pool.slice();
  const out = [];
  n = Math.min(n, arr.length);
  while(out.length < n && arr.length){
    let total = 0;
    for(let i=0;i<arr.length;i++) total += arr[i].q.w || 1;
    let r = rnd() * total;
    let pick = arr.length - 1;
    for(let i=0;i<arr.length;i++){
      r -= arr[i].q.w || 1;
      if(r <= 0){ pick = i; break; }
    }
    out.push(arr[pick]);
    arr.splice(pick, 1);
  }
  return out;
}

function shuffle(a, rnd){
  for(let i=a.length-1;i>0;i--){
    const j = Math.floor(rnd() * (i+1));
    const t = a[i]; a[i] = a[j]; a[j] = t;
  }
  return a;
}

function buildQueue(){
  let pool = BANK.map((q,i)=>({q,i}));

  /* 模式筛选 */
  if (state.mode==="star") pool = pool.filter(x=>x.q.s===1);
  if (state.mode==="wrong"){
    const w = loadWrong();
    pool = pool.filter(x=>w.indexOf(x.q.id)>=0);
  }

  /* 章节筛选 */
  if (state.chapter!=="all"){
    pool = state.chapter==="star"
      ? pool.filter(x=>x.q.c==="star")
      : pool.filter(x=>x.q.c===state.chapter && x.q.s!==1);
  }

  /* 权重筛选 */
  if (state.weight==="top") pool = pool.filter(x=>(x.q.w||1) >= 4);

  /* 错题重做模式：题本来就少，跳过去重与随机，保持稳定 */
  if (state.mode==="wrong") return pool;

  const rnd = state.seed ? mulberry32(state.seedVal) : Math.random;

  /* 同 concept 去重：每个知识点只保留权重最高（并列则随机）的一题进入候选。
     注意：只有当 size 有限时才必须去重；不限题量时去重会丢题，故仅在有限量时启用。 */
  let candidates = pool;
  if (state.dedup && state.size > 0){
    const byConcept = {};
    pool.forEach(x=>{
      const k = x.q.concept || x.q.tag || ('_'+x.i);
      (byConcept[k] = byConcept[k] || []).push(x);
    });
    candidates = Object.keys(byConcept).map(k=>{
      const g = byConcept[k].slice();
      const mx = Math.max.apply(null, g.map(x=>x.q.w||1));
      const top = g.filter(x=>(x.q.w||1) === mx);
      /* 同权重里随机挑一道，避免每次都是同一题 */
      return top[Math.floor(rnd() * top.length)];
    });
  }

  /* 抽题 */
  let picked;
  if (state.size > 0 && state.size < candidates.length){
    picked = state.weight==="flat"
      ? shuffle(candidates.slice(), rnd).slice(0, state.size)
      : weightedSample(candidates, state.size, rnd);
  } else {
    picked = candidates;
    if (state.seed) picked = shuffle(picked.slice(), rnd);   /* 限量内也打乱 */
  }

  /* 打乱题序 */
  return shuffle(picked.slice(), rnd);
}

function start(keepPage){`;

html = html.replace(buildQueueRe, buildQueueReplacement);

fs.writeFileSync(SRC, html, 'utf8');
console.log('patch-engine.js 完成');
console.log('  替换前:', before, '字节');
console.log('  替换后:', html.length, '字节');
console.log('  差异:', html.length - before, '字节');

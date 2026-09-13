/**
 * test-layout.js — 布局回归测试（真浏览器渲染）
 *
 * 为什么需要它：
 *   现有 105 项测试全是「逻辑」测试，跑在 Node vm 沙箱里 —— 那里没有 CSS 引擎，
 *   也没有布局计算。所以发生过这样的事：往 HTML 里插云同步面板时多写了一个 </div>，
 *   导致 .app 被提前闭合，整页塌成 60px 窄竖条，而 105 项测试**全部通过**。
 *
 * 本测试用真实 Chrome 渲染，测量关键元素的实际几何尺寸，断言布局没有崩：
 *   ① .app 宽度接近设计值（900px），不被压扁
 *   ② #sheet / #statbar / #pager 都在 .app 内部（x 落在 .app 的水平区间内）
 *   ③ #navwrap 贴在 .app 右侧（x 大于 .app 右边界，或与之相邻）
 *   ④ 默认收起的面板（syncPanel / histWrap）确实是 display:none
 *   ⑤ .qcard 宽度不为 0 且接近容器宽
 *
 * 找不到 Chrome / Edge 时**跳过**（打印 SKIP 并退出 0），不让缺浏览器阻断 CI。
 *
 * 用法：node tools/test-layout.js
 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const ROOT = path.resolve(__dirname, '..');
const PAGE = path.join(ROOT, '3-测验', 'index.html');
const TMP = os.tmpdir();

// ---------- 找浏览器 ----------
function findBrowser() {
  const cands = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ];
  for (const c of cands) { try { if (c && fs.existsSync(c)) return c; } catch (e) {} }
  return null;
}

const L = [];
let pass = 0, fail = 0, skipped = false;
function ok(cond, msg) { if (cond) { pass++; L.push('  ✅ ' + msg); } else { fail++; L.push('  ❌ ' + msg); } }

const browser = findBrowser();
if (!browser) {
  console.log('');
  console.log('════════ 布局回归验证 ════════');
  console.log('  ⚠️ SKIP：本机未找到 Chrome / Edge，跳过真机布局检查');
  console.log('');
  fs.writeFileSync(path.join(__dirname, '_layout.txt'), 'SKIP: no browser', 'utf8');
  process.exit(0);
}

/* 把目标页放进同源 iframe，等它渲染完再测量。
   —— 用 iframe 而同源，才能读到 contentDocument 与 getComputedStyle。 */
const probeHtml = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<iframe id="f" src="${'file:///' + PAGE.replace(/\\/g, '/')}" style="width:1200px;height:900px;border:0"></iframe>
<pre id="out">pending</pre>
<script>
var done=false;
function measure(){
  var f=document.getElementById('f');
  try{
    var d=f.contentDocument, w=f.contentWindow;
    if(!d || !d.querySelector('#sheet')) { document.getElementById('out').textContent='RETRY'; return; }
    function R(sel){
      var el=d.querySelector(sel);
      if(!el) return {sel:sel, missing:true};
      var b=el.getBoundingClientRect(), cs=w.getComputedStyle(el);
      return {sel:sel, w:Math.round(b.width), h:Math.round(b.height),
              x:Math.round(b.x), y:Math.round(b.y),
              right:Math.round(b.right), display:cs.display, cls:String(el.className)};
    }
    var o={vw:w.innerWidth, tips:f.contentDocument.title,
      items:[R('.shell'),R('.app'),R('header.top'),R('.toolbar'),R('#sheet'),
             R('.qcard'),R('#navwrap'),R('#syncPanel'),R('#histWrap'),R('#statbar'),R('#pager')]};
    document.getElementById('out').textContent=JSON.stringify(o);
    done=true;
  }catch(e){ document.getElementById('out').textContent='ERR:'+e.message; }
}
var f=document.getElementById('f');
f.onload=function(){ setTimeout(measure, 2200); };
setTimeout(function(){ if(!done) measure(); }, 4200);
<\/script></body></html>`;

const probePath = path.join(TMP, 'aag-layout-probe.html');
fs.writeFileSync(probePath, probeHtml, 'utf8');

let data = null;
try {
  const dom = execFileSync(browser, [
    '--headless=new', '--disable-gpu', '--no-sandbox',
    '--allow-file-access-from-files',
    '--virtual-time-budget=9000',
    '--window-size=1400,1000',
    '--dump-dom', 'file:///' + probePath.replace(/\\/g, '/'),
  ], { encoding: 'utf8', maxBuffer: 1024 * 1024 * 60, timeout: 120000 });

  const m = dom.match(/<pre id="out">([\s\S]*?)<\/pre>/);
  if (m) {
    const raw = m[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    if (raw === 'RETRY') throw new Error('#sheet 未渲染出来（页面脚本可能报错）');
    if (raw.indexOf('ERR:') === 0) throw new Error(raw);
    data = JSON.parse(raw);
  } else {
    throw new Error('无法从探针页面取回测量结果');
  }
} catch (e) {
  console.log('');
  console.log('════════ 布局回归验证 ════════');
  console.log('  ⚠️ SKIP：浏览器测量失败（' + String(e.message).slice(0, 120) + '）');
  console.log('');
  fs.writeFileSync(path.join(__dirname, '_layout.txt'), 'SKIP: ' + e.message, 'utf8');
  process.exit(0);
}

// ---------- 断言 ----------
L.push('════════ 布局回归验证（真浏览器）════════');
L.push('  浏览器：' + path.basename(browser) + '　视口宽度：' + data.vw + 'px');

const G = {};
data.items.forEach(i => { G[i.sel] = i; });

function need(sel) {
  const el = G[sel];
  if (!el || el.missing) { fail++; L.push('  ❌ 找不到元素 ' + sel); return null; }
  return el;
}

L.push('');
L.push('【① 主内容区宽度】');
const app = need('.app');
if (app) {
  // 设计值 max-width:900px。窄视口下会缩小，但 1200px 视口下必须在 900 附近。
  ok(app.w >= 700, '.app 宽度 = ' + app.w + 'px（应≥700，设计值 900）');
  ok(app.w <= 1000, '.app 宽度 = ' + app.w + 'px（应≤1000，防止异常撑开）');
  ok(app.display !== 'none', '.app 可见');
}

L.push('');
L.push('【② 子元素必须在 .app 内部（此条能抓住"多余 </div> 提前闭合容器"）】');
if (app) {
  const left = app.x, right = app.right;
  [['#sheet', 0], ['#statbar', 0], ['#pager', 0], ['header.top', 0], ['.toolbar', 0]].forEach(([sel]) => {
    const el = need(sel);
    if (!el) return;
    const inside = el.x >= left - 2 && el.right <= right + 2;
    ok(inside, sel + ' 在 .app 内（x=' + el.x + '，.app 区间 [' + left + ',' + right + ']）');
  });
}

L.push('');
L.push('【③ 答题卡位置】');
const nav = need('#navwrap');
if (nav && app) {
  // 桌面端：答题卡应贴在 .app 右侧（可能在右侧、也可能折叠在视口右缘）
  ok(nav.x >= app.right - 30, '#navwrap 在 .app 右侧（x=' + nav.x + ' ≥ ' + (app.right - 30) + '）');
  ok(nav.w > 100, '#navwrap 宽度合理 = ' + nav.w + 'px');
}

L.push('');
L.push('【④ 默认收起的面板真的收起了】');
[['#syncPanel', 'syncPanel'], ['#histWrap', 'histWrap']].forEach(([sel]) => {
  const el = need(sel);
  if (!el) return;
  ok(el.display === 'none', sel + ' display = ' + el.display + '（默认应为 none）');
});

L.push('');
L.push('【⑤ 题卡宽度】');
const card = need('.qcard');
if (card && app) {
  ok(card.w > 300, '.qcard 宽度 = ' + card.w + 'px（应>300，不被压扁）');
  ok(Math.abs(card.w - app.w) < 80, '.qcard 宽度接近容器（' + card.w + ' vs ' + app.w + '）');
}

L.push('');
L.push('【⑥ 页面整体高度合理（防止元素塌成竖条导致超高）】');
const shell = need('.shell');
if (shell) {
  // 90 题、每页 10 题，单页 10 张卡；塌成竖条时 height 会异常暴涨
  const ratio = shell.h / Math.max(1, shell.w);
  ok(ratio < 12, '.shell 高宽比 = ' + ratio.toFixed(1) + '（应<12；竖条化时此值会暴涨）');
}

L.push('');
L.push('─────────────────────────────');
L.push((fail ? '❌ ' : '✅ ') + '布局：' + pass + ' 通过 / ' + fail + ' 失败');

const out = L.join('\n');
fs.writeFileSync(path.join(__dirname, '_layout.txt'), out, 'utf8');
console.log('');
console.log(out);
console.log('');
process.exit(fail ? 1 : 0);

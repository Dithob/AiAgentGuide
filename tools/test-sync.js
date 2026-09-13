/**
 * test-sync.js — 云同步（GitHub Gist）验证
 *
 * 用一个假的 api.github.com 模拟 GitHub，验证：
 *   ① 连接：token 校验（401 报错、正常通过）
 *   ② 上传：首次连接自动建 Gist；再次上传走 PATCH
 *   ③ 下载：云端数据正确覆盖本机（错题池 / 组卷参数 / 历史）
 *   ④ 冲突：云端更新时上传会弹确认；取消则中止
 *   ⑤ 自动上传：本机无改动不触发；有改动且云端不新才推
 *   ⑥ 凭证隔离：payload 里绝不能出现 token / gistId（最重要的安全断言）
 *   ⑦ 设备关联：只有 token 没有 gistId 时，靠描述找回同一份 Gist
 *   ⑧ 安静通道：同步自身写状态不会把自己判成「本机有改动」而形成死循环
 *
 * 用法：node tools/test-sync.js
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
    id, _html: '', textContent: '', value: '', checked: false, type: '',
    dataset: {}, style: {},
    classList: { _s: new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
                 toggle(c,f){ f===undefined ? (this._s.has(c)?this._s.delete(c):this._s.add(c)) : (f?this._s.add(c):this._s.delete(c)); },
                 contains(c){ return this._s.has(c); } },
    scrollIntoView(){}, appendChild(){}, querySelectorAll(){ return []; },
    get innerHTML(){ return this._html; },
    set innerHTML(v){ this._html = String(v); },
  };
  for (const ev of ['onclick','onchange','oninput']) {
    Object.defineProperty(el, ev, { set(v){ el['_'+ev]=v; }, get(){ return el['_'+ev]; }, configurable: true });
  }
  el.addEventListener = () => {};
  return el;
}

const store = {};
const errors = [];

// ---------- 假 GitHub API ----------
const GH = {
  gists: {},          // id -> {id, description, updated_at, files}
  seq: 0,
  calls: [],          // {method, path, body}
  tokenOk: 'ghp_valid_token_1234567890',
  failMode: null,     // null | 'unauthorized' | 'forbidden' | 'notfound'
};
function ghReset(){ GH.gists = {}; GH.seq = 0; GH.calls = []; GH.failMode = null; }

function mockFetch(url, opts) {
  opts = opts || {};
  const method = (opts.method || 'GET').toUpperCase();
  const u = String(url);
  const hdrs = opts.headers || {};
  const auth = hdrs['Authorization'] || hdrs['authorization'] || '';
  const token = auth.replace('Bearer ', '');
  const body = opts.body ? JSON.parse(opts.body) : null;
  GH.calls.push({ method, url: u, body });

  const respond = (status, obj) => Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(obj),
    text: () => Promise.resolve(JSON.stringify(obj)),
  });

  // 只处理 api.github.com
  if (u.indexOf('https://api.github.com') !== 0) {
    return Promise.reject(new Error('mock 不支持该 URL: ' + u));
  }

  if (GH.failMode === 'unauthorized' || token !== GH.tokenOk) return respond(401, { message: 'Bad credentials' });
  if (GH.failMode === 'forbidden') return respond(403, { message: 'Forbidden' });
  if (GH.failMode === 'notfound') return respond(404, { message: 'Not Found' });

  const path_ = u.replace('https://api.github.com', '');

  // GET /gists?per_page=... → 列表
  if (/^\/gists\?/.test(path_) && method === 'GET') {
    const list = Object.keys(GH.gists).map(k => ({
      id: k, description: GH.gists[k].description, updated_at: GH.gists[k].updated_at,
    }));
    return respond(200, list);
  }
  // POST /gists → 新建
  if (path_ === '/gists' && method === 'POST') {
    GH.seq++;
    const id = 'gist_' + GH.seq;
    GH.gists[id] = { id, description: body.description, updated_at: new Date().toISOString(), files: body.files };
    return respond(201, GH.gists[id]);
  }
  // GET /gists/{id}
  let m = path_.match(/^\/gists\/([^/?]+)$/);
  if (m && method === 'GET') {
    const g = GH.gists[m[1]];
    if (!g) return respond(404, { message: 'Not Found' });
    return respond(200, g);
  }
  // PATCH /gists/{id}
  if (m && method === 'PATCH') {
    const g = GH.gists[m[1]];
    if (!g) return respond(404, { message: 'Not Found' });
    if (body && body.files) g.files = body.files;
    g.updated_at = new Date().toISOString();
    return respond(200, g);
  }
  return respond(404, { message: 'Not Found' });
}

let confirmQueue = [];
let confirmed = [];
const alerts = [];

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
  fetch: mockFetch,
  confirm(msg){ confirmed.push(msg); return confirmQueue.length ? confirmQueue.shift() : true; },
  alert(m){ alerts.push(m); },
  setTimeout(fn, ms){ return global.setTimeout(fn, 0); },
  clearTimeout(t){ return global.clearTimeout(t); },
};
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(code, ctx, { filename: 'index.html<script>' });
} catch (e) {
  errors.push('脚本执行异常: ' + e.message);
}

const K = { WRONG:'aag_quiz_wrong_v3', COMPOSE:'aag_quiz_compose_v1', CS:'aag_quiz_cs_v1', HIST:'aag_quiz_hist_v1' };
const getCS = () => { try { return JSON.parse(store[K.CS] || '{}'); } catch (e) { return {}; } };

setTimeout(async () => {
  L.push('════════ 云同步（Gist）验证 ════════');
  if (errors.length) errors.forEach(e => L.push('  ❌ 运行期异常: ' + e));
  else L.push('  ✅ 脚本加载无异常');

  const T = sandbox.__t;
  if (!T) { L.push('  ❌ 测试钩子未挂载'); return finish(); }
  const CS = T.cs();
  ok(typeof CS.upload === 'function' && typeof CS.download === 'function', 'CloudSync 已导出');

  // 先造一点本机数据
  T.saveWrong(['ch00-01', 'ch01-03']);
  store[K.COMPOSE] = JSON.stringify({ size: 20, weight: 'weighted', dedup: true, seed: true });
  T.pushHist({ t: Date.now(), total: 20, graded: 20, right: 17, pct: 85, mode: 'all' });

  // ---------- ① 连接 ----------
  L.push('');
  L.push('【① token 校验】');
  ghReset();
  let okErr = null;
  await CS.setToken('ghp_wrong_token').catch(e => { okErr = e; });
  ok(okErr && /401|无效/.test(okErr.message), '错误 token 被拒：' + (okErr && okErr.message));

  okErr = null;                        /* 必须重置，否则残留上一次的错误 */
  await CS.setToken(GH.tokenOk).catch(e => { okErr = e; });
  ok(!okErr, '正确 token 通过校验' + (okErr ? '（实际报错：' + okErr.message + '）' : ''));
  ok(getCS().token === GH.tokenOk, 'token 已存本机');
  ok(GH.calls.some(c => /\/gists\?per_page=1$/.test(c.url)), '校验走的是 GET /gists?per_page=1');

  // ---------- ② 上传 ----------
  L.push('');
  L.push('【② 上传到云端】');
  GH.gists = {};                      /* 清空，验证会新建 */
  T.saveCS({ gistId: '' }, true);
  confirmQueue = [true];
  await CS.upload();
  const created = Object.keys(GH.gists);
  ok(created.length === 1, '首次上传新建了 1 个 Gist（共 ' + created.length + '）');
  const g0 = GH.gists[created[0]];
  ok(g0.description === T.csKeys().GIST_DESC, 'Gist 描述正确：' + g0.description);
  const up1 = JSON.parse(g0.files[T.csKeys().GIST_FILE].content);
  ok(Array.isArray(up1.wrong) && up1.wrong.length === 2, '上传的 wrong 有 2 条');
  ok(up1.compose && up1.compose.size === 20, '上传的 compose.size = 20');
  ok(Array.isArray(up1.hist) && up1.hist.length >= 1, '上传的 hist 至少有 1 条');

  // ---------- ⑥ 凭证隔离（最关键） ----------
  L.push('');
  L.push('【⑥ 凭证隔离：payload 绝不能含 token】');
  const rawContent = g0.files[T.csKeys().GIST_FILE].content;
  ok(rawContent.indexOf(GH.tokenOk) < 0, 'payload 中不含 token 明文');
  ok(rawContent.indexOf('gistId') < 0 && JSON.stringify(up1).indexOf('gistId') < 0, 'payload 中不含 gistId 字段');
  const p = T.collectPayload();
  ok(Object.keys(p).sort().join(',') === 'app,compose,exportedAt,hist,version,wrong',
     'payload 顶层字段仅含白名单：' + Object.keys(p).join(','));
  ok(JSON.stringify(p).indexOf(GH.tokenOk) < 0, 'collectPayload() 不含 token');

  // 二次上传走 PATCH
  L.push('');
  L.push('【②-b 二次上传复用同一个 Gist】');
  GH.calls = [];
  T.saveWrong(['ch00-01', 'ch01-03', 'ch02-07']);   /* 改数据 */
  confirmQueue = [true];
  await CS.upload();
  ok(Object.keys(GH.gists).length === 1, '没有新建第二个 Gist');
  ok(GH.calls.some(c => c.method === 'PATCH' && /\/gists\/gist_1$/.test(c.url)), '二次上传走 PATCH /gists/{id}');
  const up2 = JSON.parse(GH.gists['gist_1'].files[T.csKeys().GIST_FILE].content);
  ok(up2.wrong.length === 3, '云端已更新为 3 条错题');

  // ---------- ③ 下载 ----------
  L.push('');
  L.push('【③ 从云端下载】');
  // 模拟另一台设备：云端是一份不同数据
  GH.gists['gist_1'].files[T.csKeys().GIST_FILE].content = JSON.stringify({
    app: 'aag-quiz', version: 1,
    exportedAt: new Date(Date.now() + 60000).toISOString(),
    wrong: ['ch03-01', 'ch03-02', 'ch03-03', 'ch03-04'],
    compose: { size: 10, weight: 'top', dedup: false, seed: false },
    hist: [{ t: Date.now(), total: 10, graded: 10, right: 10, pct: 100, mode: 'star' }],
  });
  GH.gists['gist_1'].updated_at = new Date(Date.now() + 60000).toISOString();
  confirmQueue = [true];
  await CS.download();
  ok(JSON.stringify(T.loadWrong()) === JSON.stringify(['ch03-01','ch03-02','ch03-03','ch03-04']),
     '错题池已被云端覆盖：' + JSON.stringify(T.loadWrong()));
  const st = T.state();
  ok(st.size === 10 && st.weight === 'top' && st.dedup === false, '组卷参数已被云端覆盖');
  ok(T.loadHist().length === 1 && T.loadHist()[0].pct === 100, '答题历史已被云端覆盖');

  // ---------- ④ 冲突：云端更新时取消上传 ----------
  L.push('');
  L.push('【④ 冲突处理：云端更新时拒绝覆盖】');
  // 让本地"改动"晚于同步点，但云端 exportedAt 更晚 → 应弹确认
  T.saveCS({ syncedAt: 0 }, true);
  const wrongBefore = JSON.stringify(T.loadWrong());
  T.saveWrong(['ch00-99']);          /* 本地改动 */
  confirmQueue = [false];            /* 用户点「取消」 */
  confirmed = [];
  await CS.upload();
  ok(confirmed.length > 0, '上传前弹出了覆盖确认框');
  ok(confirmed[0].indexOf('云端') >= 0, '确认文案提示了云端更新时间');
  const remoteNow = JSON.parse(GH.gists['gist_1'].files[T.csKeys().GIST_FILE].content);
  ok(JSON.stringify(remoteNow.wrong) === JSON.stringify(['ch03-01','ch03-02','ch03-03','ch03-04']),
     '取消后云端未被覆盖（仍是 4 条）');

  // ---------- ⑦ 设备关联 ----------
  L.push('');
  L.push('【⑦ 新设备靠描述找回同一份 Gist】');
  T.saveCS({ gistId: '' }, true);    /* 模拟新设备：只有 token，没有 gistId */
  ok(getCS().gistId === '', '已清空 gistId（模拟新设备）');
  GH.calls = [];
  confirmQueue = [true];
  await CS.download();
  ok(getCS().gistId === 'gist_1', '自动关联到已存在的 Gist（未新建）：' + getCS().gistId);
  ok(Object.keys(GH.gists).length === 1, 'Gist 总数仍是 1（没有误建第二个）');

  // ---------- ⑤ 自动上传 ----------
  L.push('');
  L.push('【⑤ 自动上传的触发条件】');
  // 场景 A：本机无改动（lastLocalWrite <= syncedAt）→ autoPushNow 应直接返回、不发请求
  T.saveCS({ token: GH.tokenOk, autoPush: true, syncedAt: Date.now() + 10000 }, true);
  const csA = getCS();
  ok((csA.lastLocalWrite || 0) <= (csA.syncedAt || 0),
     '构造出「本机无改动」状态（lw=' + (csA.lastLocalWrite||0) + ' ≤ sa=' + csA.syncedAt + '）');
  // 直接驱动自动上传：无改动 → 不应产生任何 GitHub 调用
  GH.calls = [];
  CS.scheduleAutoPush();
  await new Promise(r => setTimeout(r, 60));
  ok(GH.calls.length === 0, '无本机改动时自动上传不发请求（0 次调用）');

  // 场景 B：本机有改动且云端不新 → 应推送
  T.saveCS({ token: GH.tokenOk, autoPush: true, syncedAt: 0 }, false);  /* 非 silent → 刷新 lw */
  GH.calls = [];
  CS.scheduleAutoPush();
  await new Promise(r => setTimeout(r, 60));
  ok(GH.calls.length > 0, '有本机改动时自动上传会推送到云端（' + GH.calls.length + ' 次调用）');

  // ---------- ⑧ 安静通道 ----------
  L.push('');
  L.push('【⑧ 安静通道：同步自身写状态不算本机改动】');
  const lwBefore = getCS().lastLocalWrite || 0;
  T.saveCS({ syncedAt: 123456 }, true);   /* silent=true */
  ok((getCS().lastLocalWrite || 0) === lwBefore, 'silent 写入不更新 lastLocalWrite');
  T.saveCS({ syncedAt: 123457 }, false);  /* silent=false */
  ok((getCS().lastLocalWrite || 0) > lwBefore, '非 silent 写入会更新 lastLocalWrite');

  // ---------- ⑨ 错误处理 ----------
  L.push('');
  L.push('【⑨ GitHub API 错误处理】');
  GH.failMode = 'forbidden';
  let e403 = null;
  await CS.upload().catch(e => { e403 = e; });
  const stText = els['csStatus'] ? els['csStatus'].innerHTML + els['csStatus'].textContent : '';
  ok(/403|权限|限流/.test(stText), '403 被如实提示到状态栏：' + stText.slice(0, 60));
  GH.failMode = null;

  // ---------- ⑩ 时区/格式 ----------
  L.push('');
  L.push('【⑩ 载荷格式】');
  const pl = T.collectPayload();
  ok(typeof pl.exportedAt === 'string' && !isNaN(Date.parse(pl.exportedAt)), 'exportedAt 是合法 ISO 时间');
  ok(pl.app === 'aag-quiz' && pl.version === 1, 'app/version 标识正确');

  finish();
}, 200);

function finish() {
  L.push('');
  L.push('─────────────────────────────');
  L.push((fail ? '❌ ' : '✅ ') + '云同步：' + pass + ' 通过 / ' + fail + ' 失败');
  const out = L.join('\n');
  fs.writeFileSync(path.join(__dirname, '_sync.txt'), out, 'utf8');
  console.log(out);
  process.exit(fail ? 1 : 0);
}

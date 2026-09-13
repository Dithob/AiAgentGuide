/**
 * build.js — 把 bank.json 内联进 index.html（双形态构建）
 *
 * 为什么需要这一步：
 *   题库外置成 bank.json 后，`fetch('./bank.json')` 在 file:// 下会被浏览器拦截
 *   （file:// 是 opaque origin，CORS 只支持 http/https）。
 *   而"双击即用"是本项目承诺的核心体验，不能丢。
 *
 * 解法：把 bank.json 的 JSON 直接内联进 index.html 的 BANK_INLINE，
 *   ① 内联有数据 → 双击 file:// 直接可用
 *   ② 内联为空   → 部署形态下 fetch ./bank.json
 * 两个产物（bank.json + index.html）都提交到仓库。
 *
 * 同时写入 BANK_OLD_ORDER_IDS —— 旧版错题池存的是数组下标，
 * 需要一个「旧顺序 → id」的快照才能正确迁移。
 * 该快照从 .index.html.bak（改造前的原始文件）中提取，只在首次构建时生成。
 *
 * 用法：node tools/build.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, '3-测验', 'index.html');
const BANK = path.join(ROOT, '3-测验', 'bank.json');
const BAK = path.join(ROOT, '3-测验', '.index.html.bak');

/* ---------- 1. 读 bank.json ---------- */
if (!fs.existsSync(BANK)) throw new Error('缺少 bank.json，请先运行 node tools/export-bank.js');
const bank = JSON.parse(fs.readFileSync(BANK, 'utf8'));
if (!Array.isArray(bank) || !bank.length) throw new Error('bank.json 为空');

/* ---------- 2. 提取旧顺序快照（用于错题池迁移） ---------- */
// 从改造前的备份里抽出旧 BANK 的 id 顺序。若备份不存在（如已清理），
// 则退化为「用当前 bank 顺序」——只影响一次性迁移的准确性，不影响新功能。
let oldOrder = [];
if (fs.existsSync(BAK)) {
  const bak = fs.readFileSync(BAK, 'utf8');
  const m = bak.match(/const BANK = (\[[\s\S]*?\]);\n/);
  if (m) {
    const oldBank = JSON.parse(m[1]);
    // 旧题库没有 id 字段，但顺序与新 bank 的前 N 题一致（新 bank 是旧 bank 加了字段派生的）
    oldOrder = oldBank.map((_, i) => (bank[i] && bank[i].id) || null).filter(Boolean);
  }
}
if (!oldOrder.length) oldOrder = bank.map(x => x.id);

/* ---------- 3. 注入 index.html ---------- */
let html = fs.readFileSync(HTML, 'utf8');
const bankJson = JSON.stringify(bank);
const oldJson = JSON.stringify(oldOrder);

// BANK_INLINE
const bankSlot = /const BANK_INLINE = \/\*__BANK_INLINE__\*\/[\s\S]*?\/\*__END_BANK_INLINE__\*\/;/;
if (!bankSlot.test(html)) throw new Error('未找到 BANK_INLINE 占位标记');
html = html.replace(bankSlot,
  'const BANK_INLINE = /*__BANK_INLINE__*/' + bankJson + '/*__END_BANK_INLINE__*/;');

// BANK_OLD_ORDER_IDS
const oldSlot = /const BANK_OLD_ORDER_IDS = \/\*__OLD_ORDER__\*\/[\s\S]*?\/\*__END_OLD_ORDER__\*\/;/;
if (!oldSlot.test(html)) throw new Error('未找到 BANK_OLD_ORDER_IDS 占位标记');
html = html.replace(oldSlot,
  'const BANK_OLD_ORDER_IDS = /*__OLD_ORDER__*/' + oldJson + '/*__END_OLD_ORDER__*/;');

fs.writeFileSync(HTML, html, 'utf8');

/* ---------- 4. 校验 ---------- */
const check = fs.readFileSync(HTML, 'utf8');
const inlineMatch = check.match(/const BANK_INLINE = \/\*__BANK_INLINE__\*\/(\[[\s\S]*?\])\/\*__END_BANK_INLINE__\*\//);
if (!inlineMatch) throw new Error('注入后校验失败：BANK_INLINE 不合法');
const injected = JSON.parse(inlineMatch[1]);
if (injected.length !== bank.length) {
  throw new Error('注入题量不一致: ' + injected.length + ' vs ' + bank.length);
}

console.log('build.js 完成');
console.log('  题库题数:', bank.length);
console.log('  内联 JSON:', (bankJson.length / 1024).toFixed(1), 'KB');
console.log('  旧顺序快照:', oldOrder.length, '条');
console.log('  index.html:', (fs.statSync(HTML).size / 1024).toFixed(1), 'KB');

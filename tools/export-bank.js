/**
 * export-bank.js — 重算全部派生字段（id / concept / w），覆盖写 bank.json
 *
 * ⚠️ 覆盖写！它会按下面的推导规则**重新计算每一题的 w**，
 *    因此在 bank.json 里手工调过的权重会被抹掉。跑之前先备份。
 *    常规改题**不需要**跑这个脚本 —— bank.json 才是唯一编辑入口，
 *    改完直接 `npm run build && npm test` 即可。
 *
 * 用法：node tools/export-bank.js
 *
 * 数据来源：3-测验/index.html 里内联的 BANK_INLINE
 *   （早期版本 index.html 里是 `const BANK = [...]`，现已改为内联占位标记，
 *     由 tools/build.js 注入。本脚本已同步适配。）
 *
 * 补齐字段：
 *   id       稳定唯一 ID（章节-序号），用于错题池持久化（替代不稳定的数组下标）
 *   concept  知识点 ID（由 tag 归一化而来），用于「同知识点不在一套题里重复」
 *   w        权重 1~5（置信度/重点程度），按推导规则自动计算
 *
 * 权重推导规则（用户已认可，实施时做了两点校准）：
 *   基础分 1
 *   + 2  你标过 == 的强化点（s:1）
 *   + 2  出现在错题台账里
 *   + 1  lv 为 L3（应用层——README 定义的「训练目标」线）
 *   + 2  lv 为 L4（设计/权衡层——README 定义的「分水岭」）
 *   + 1  有对应的开放题 O-xx（说明是面试会问的）
 *   上限 5
 *
 * 为什么把基础分从 2 降到 1、并让 lv 参与计分：
 *   实测若基础分=2 且 star 独得 +2，则 18 道 🔆 全部并列 w=4，
 *   而真正是面试重点的 L3 题（如 ch03-14 自主编排的边界）只有 2 分
 *   → 「重点」退化成「是不是派生题」，失去区分度。
 *   让 lv 按 README 自己的四级深度表计分后，权重才反映「面试重要性」。
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SRC = path.join(ROOT, '3-测验', 'index.html');
const OUT = path.join(ROOT, '3-测验', 'bank.json');

/* ---------- 0. 安全闸门 ---------- */
// 本脚本会按规则重算每一题的 w 并覆盖 bank.json。手工调过的权重一旦被误跑抹掉，
// 很难还原，所以要求显式 --force。（2026-09-18 加：bank.json 已是唯一编辑入口）
if (!process.argv.includes('--force')) {
  console.error('拒绝执行：本脚本会覆盖写 bank.json，并按规则重算全部 w。');
  console.error('  常规改题：直接编辑 3-测验/bank.json → node tools/build.js → node tools/run-tests.js');
  console.error('  确实要重算：node tools/export-bank.js --force');
  process.exit(1);
}

/* ---------- 1. 抽出原始 BANK ---------- */
const html = fs.readFileSync(SRC, 'utf8');
const m = html.match(/const BANK_INLINE = \/\*__BANK_INLINE__\*\/(\[[\s\S]*?\])\/\*__END_BANK_INLINE__\*\//);
if (!m) throw new Error('未能在 index.html 中定位 BANK_INLINE 内容（先跑 node tools/build.js）');
const raw = JSON.parse(m[1]);
console.log('抽出题目数:', raw.length);

/* ---------- 2. concept 归一化表 ---------- */
// 实测：80 个 tag 中 7 组重复。把语义相同的合并成同一 concept。
// 左边是原 tag，右边是归一化后的 concept（保留可读性，不缩写）。
const CONCEPT_MAP = {
  // 「适用边界」4 题：讲的是同一件事——什么时候该用 Agent，什么时候不该
  '适用边界': 'Agent 适用边界',
  // 「Agent 本质」2 题：Agent 与 ChatBot / 普通 LLM 的本质差异
  'Agent 本质': 'Agent 本质与差异',
  // 「OODA 与回边」2 题：核心闭环与反馈回边
  'OODA 与回边': '核心闭环与回边',
  // 「自主性光谱」2 题：ChatBot → Copilot → Agent 的自主性递进
  '自主性光谱': '自主性光谱',
  // 「description 为王」3 题：工具 Schema 的 description 字段
  'description 为王': '工具 description',
  // 下面两组是 ch03 天气 Agent 场景，语义上各自成组
  '指数退避': '异常重试策略',
  '错误也要告诉 AI': '异常处理原则',
};

/* ---------- 3. 错题台账（来自 01-选择题 的「我的答题记录」表） ---------- */
// 实测：台账里 2 条 —— 余弦相似度值域、128K Token 分配。
// 它们对应的题目（用于 +2 权重）。
const ERRORLOG_CONCEPTS = [
  '余弦值域',       // 「余弦 / 欧氏 / 点积怎么选」→ star-17
  '缓存分层',       // 「128K 窗口分配 / 缓存分层」→ star-14
];

/* ---------- 4. 开放题 O-xx 覆盖的知识点 ---------- */
// 来自 02-开放题-口述版.md（O-01 ~ O-30）的关联条目。用于 +1 权重。
// 注意：必须用剥离 🔆 前缀后的真实 concept 名。
const OQ_CONCEPTS = new Set([
  'Token 密度', 'Prompt 分层', 'Embedding 本质', '只决定不执行',
  'Attention 的收益', 'Attention 的代价', 'LSTM 的效果', 'RoPE 三大优势',
  'COT 的收益与代价', 'CoT 的收益与代价',
  '循环终止条件', '对话历史即记忆', '工具 description', '异常重试策略',
  'Agent 适用边界', '自主性光谱', '缓存分层', '余弦值域',
  '框架选择原则', '外推的现实', 'FlashAttention',
  '完整链路', '任务分类', '调度决策', '多步任务需闭环',
  '自主编排的边界', '生产化清单', 'Schema 设计四原则', '异常处理原则',
  '核心闭环与回边', '自主决策', '反思触发条件', 'Token 预算分配',
]);

/* ---------- 5. 逐题补齐字段 ---------- */
const chapterCounter = {};
const enriched = raw.map((q, i) => {
  const c = q.c || 'star';
  chapterCounter[c] = (chapterCounter[c] || 0) + 1;
  const seq = String(chapterCounter[c]).padStart(2, '0');

  const tag = q.tag || '';
  // concept：先查归一化表，否则用 tag 原文
  let concept = CONCEPT_MAP[tag] || tag;
  // 派生题的 tag 带「🔆 」前缀，统一剥掉，否则和 OQ_CONCEPTS 对不上。
  // 是不是派生题由 s 字段表示，concept 应该只描述「考什么知识点」。
  concept = concept.replace(/^🔆\s*/, '');
  // 「你的错题」也不是知识点，是来源标记，剥掉后归入真实知识点。
  concept = concept.replace(/（你的错题）$/, '');
  // ch02 的「🔁 循环终止条件」前缀同理
  concept = concept.replace(/^🔁\s*/, '');

  // 权重推导
  // 注意：源题库只标了 1 道 L4，所以「L4 才加分」无法产生区分度。
  // 实测更有效的信号是「题型」——多选几乎总在考权衡/边界（L3~L4 的实质），
  // 而单选多为定义/机制。故用 multi 作为「深度」的代理信号。
  let w = 1;                                       // 基础分
  if (q.s === 1) w += 2;                           // == 强化派生题
  if (ERRORLOG_CONCEPTS.includes(concept)) w += 2; // 曾错过的知识点
  if (q.t === 'multi') w += 1;                     // 多选：多考权衡/边界
  if (q.lv === 'L3') w += 1;                       // 应用层（README 训练目标线）
  if (q.lv === 'L4') w += 2;                       // 设计/权衡层（README 分水岭）
  if (OQ_CONCEPTS.has(concept)) w += 1;            // 有对应开放题（面试会问）
  w = Math.min(5, Math.max(1, w));                 // 夹到 1~5

  const out = {
    id: `${c}-${seq}`,                           // 稳定唯一 ID
    c,                                           // 章节
    concept,                                     // 知识点 ID（组卷去重用）
    w,                                           // 权重 1~5
    t: q.t,                                      // single | multi
    lv: q.lv,                                    // L1~L4
    tag,                                         // 原 tag（展示用，保留兼容）
    q: q.q, o: q.o, a: q.a, e: q.e,
  };
  if (q.s === 1) out.s = 1;                      // 强化派生标记
  return out;
});

/* ---------- 6. 一致性校验 ---------- */
const ids = enriched.map(x => x.id);
const dupIds = ids.filter((x, i) => ids.indexOf(x) !== i);
if (dupIds.length) throw new Error('ID 重复: ' + dupIds.join(', '));

enriched.forEach(x => {
  if (!x.q || !Array.isArray(x.o) || !Array.isArray(x.a) || !x.e) {
    throw new Error('字段缺失: ' + x.id);
  }
  if (x.a.some(j => j < 0 || j >= x.o.length)) {
    throw new Error('答案索引越界: ' + x.id);
  }
});

/* ---------- 7. 输出 ---------- */
fs.writeFileSync(OUT, JSON.stringify(enriched, null, 2) + '\n', 'utf8');

/* ---------- 8. 统计报告 ---------- */
const byW = {};
enriched.forEach(x => byW[x.w] = (byW[x.w] || 0) + 1);
const byConcept = {};
enriched.forEach(x => byConcept[x.concept] = (byConcept[x.concept] || 0) + 1);
const multiConcept = Object.entries(byConcept).filter(([, v]) => v > 1);

console.log('');
console.log('=== 输出 ===');
console.log('bank.json 题目数:', enriched.length);
console.log('文件大小:', (fs.statSync(OUT).size / 1024).toFixed(1), 'KB');
console.log('');
console.log('=== 权重分布 ===');
Object.keys(byW).sort().forEach(k => console.log('  w=' + k + ':', byW[k], '题'));
console.log('');
console.log('=== concept 分布 ===');
console.log('  唯一 concept 数:', Object.keys(byConcept).length);
console.log('  拥有多题的 concept:', multiConcept.length, '组');
multiConcept.forEach(([k, v]) => console.log('    ' + k + ' ×' + v));
console.log('');
console.log('=== ID 抽样 ===');
console.log('  ', enriched.slice(0, 4).map(x => x.id).join(', '), '...');
console.log('  ', enriched.slice(-3).map(x => x.id).join(', '));

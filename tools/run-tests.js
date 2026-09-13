/**
 * run-tests.js — 一次跑完全部回归测试
 *
 * 用法：node tools/run-tests.js
 *
 * 覆盖：
 *   ① 引擎逻辑：组卷 / 去重 / 权重 / 种子随机 / 错题池 / 迁移
 *   ② 渲染路径：boot → start → renderAll 全链路
 *   ③ 部署形态：file:// 内联 vs http:// 外置 vs 两者皆无
 *   ④ 端到端：真实答题闭环（答题→判分→错题入池→跨组卷保留→重做）
 *   ⑤ 云同步：Gist 上传/下载/冲突/凭证隔离/自动上传/安静通道
 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const DIR = __dirname;
const TESTS = [
  ['引擎逻辑', 'test-engine.js', '_test.txt'],
  ['渲染路径', 'test-render.js', '_render.txt'],
  ['部署形态', 'test-paths.js', '_paths.txt'],
  ['端到端流程', 'test-e2e.js', '_e2e.txt'],
  ['云同步', 'test-sync.js', '_sync.txt'],
];

let totalFail = 0;
const summary = [];

TESTS.forEach(([name, script, report]) => {
  let out = '';
  try {
    out = execFileSync(process.execPath, [path.join(DIR, script)], { encoding: 'utf8' });
  } catch (e) {
    out = (e.stdout || '') + (e.stderr || '');
  }
  const rptPath = path.join(DIR, report);
  const rpt = fs.existsSync(rptPath) ? fs.readFileSync(rptPath, 'utf8') : '';
  const fails = (rpt.match(/❌/g) || []).length;
  const passes = (rpt.match(/✅/g) || []).length;
  totalFail += fails;
  summary.push({ name, passes, fails });
});

console.log('');
console.log('════════ 回归测试汇总 ════════');
summary.forEach(s => {
  console.log('  ' + (s.fails ? '❌' : '✅') + ' ' + s.name +
    '：' + s.passes + ' 通过' + (s.fails ? '，' + s.fails + ' 失败' : ''));
});
console.log('─────────────────────────────');
console.log(totalFail ? ('共 ' + totalFail + ' 项失败') : '全部通过 ✅');
console.log('');
process.exit(totalFail ? 1 : 0);

/**
 * SpaceSight 前端行为验证（CDP 驱动真实浏览器）
 * 复现用户路径：处于「矩形图」视图 → 手动添加一个追踪目标 → 观察矩形图是否切换并渲染。
 *
 * 用法： node tools/cdp-test.mjs <测试目录> [截图输出路径]
 */
import fs from 'node:fs';

const FIXTURE = process.argv[2] || '';
const SHOT = process.argv[3] || '';
const LABEL = 'UI 测试目标 ' + String(Date.now() % 100000);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? '✓' : '✗'} ${name}${ok ? '' : '  -> ' + detail}`);
};

const list = await (await fetch('http://127.0.0.1:9333/json/list')).json();
const page = list.find(t => t.type === 'page' && String(t.url).includes('8765'));
if (!page) throw new Error('找不到页面目标: ' + JSON.stringify(list.map(t => t.url)));
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
let seq = 0;
const pending = new Map();
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise(res => {
  const id = ++seq;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});
async function evaluate(expression, awaitPromise = false) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true });
  const ex = r.result?.exceptionDetails;
  if (ex) throw new Error(ex.exception?.description || ex.text || 'evaluate failed');
  return r.result?.result?.value;
}

await send('Runtime.enable');
await send('Page.enable');
await sleep(1500);

console.log('\n[准备]');
const booted = await evaluate('typeof addRoot === "function" && typeof S === "object" && S.booted === true');
check('页面已加载并完成启动', booted === true, String(booted));
const view = await evaluate('S.view');
check('当前视图为矩形图', view === 'treemap', view);
const before = await evaluate('({ crumb: document.querySelector("#tm-crumb").textContent.trim(), cells: document.querySelectorAll("#treemap .tm-cell").length, roots: S.roots.length, tmRoot: S.tmRootId })');
console.log('  添加前：', JSON.stringify(before));

console.log('\n[添加追踪目标]');
await evaluate(`window.__addDone = false; addRoot(${JSON.stringify(FIXTURE)}, ${JSON.stringify(LABEL)}).then(() => { window.__addDone = true; }); true`);
for (let i = 0; i < 40; i++) {
  await sleep(250);
  if (await evaluate('window.__addDone === true')) break;
}
check('addRoot 调用完成', (await evaluate('window.__addDone === true')) === true);

// 等扫描结束（最多 12s）
for (let i = 0; i < 24; i++) {
  const st = await evaluate('({ status: (S.rootMap.get(S.tmRootId)||{}).status, cells: document.querySelectorAll("#treemap .tm-cell").length })');
  if (st.cells > 0) break;
  await sleep(500);
}

console.log('\n[验证矩形图]');
const after = await evaluate('({ crumb: document.querySelector("#tm-crumb").textContent.trim(), cells: document.querySelectorAll("#treemap .tm-cell").length, lvl2: document.querySelectorAll("#treemap .tm-cell.lvl2").length, tmRoot: S.tmRootId, active: S.activeRoot, highlight: document.querySelectorAll(".root-card.active").length, empty: !!document.querySelector(".tm-empty") })');
console.log('  添加后：', JSON.stringify(after));
check('矩形图切换到了新目标', after.crumb.includes(LABEL), after.crumb);
check('矩形图渲染出方块', after.cells > 0, `cells=${after.cells}`);
check('渲染出第二层方块', after.lvl2 > 0, `lvl2=${after.lvl2}`);
check('S.tmRootId 指向新目标', after.tmRoot !== before.tmRoot, `${before.tmRoot} -> ${after.tmRoot}`);
check('左侧卡片高亮同步', after.active === after.tmRoot && after.highlight === 1, JSON.stringify({ active: after.active, highlight: after.highlight }));
check('未出现空状态提示', after.empty === false, String(after.empty));

// 校验总大小与内容（前端状态在扫描结束后由任务轮询回填，最多等 6 秒）
const expectedBytes = (640 + 1800 + 760 + 320 + 3) * 1024 * 1024;
let realBytes = 0;
for (let i = 0; i < 24; i++) {
  realBytes = await evaluate('(S.rootMap.get(S.tmRootId)||{}).total_bytes || 0');
  if (Math.abs(realBytes - expectedBytes) < 1024 * 1024) break;
  await sleep(250);
}
const total = await evaluate('fmtBytes((S.rootMap.get(S.tmRootId)||{}).total_bytes || 0)');
check('扫描大小与样本一致', Math.abs(realBytes - expectedBytes) < 1024 * 1024, `${realBytes} vs ${expectedBytes} (${total})`);

// 稳定性：等 8 秒，确认矩形图没有被周期性重建（无闪烁）
const cellsBefore = after.cells;
const stampBefore = await evaluate('document.querySelector("#treemap .tm-cell") ? document.querySelector("#treemap .tm-cell").style.animationDelay : ""');
await sleep(8000);
const afterIdle = await evaluate('({ cells: document.querySelectorAll("#treemap .tm-cell").length, stamp: document.querySelector("#treemap .tm-cell") ? document.querySelector("#treemap .tm-cell").style.animationDelay : "" })');
check('空闲 8 秒后矩形图未被重建（无周期性闪烁）', afterIdle.cells === cellsBefore && afterIdle.stamp === stampBefore, JSON.stringify({ before: { cells: cellsBefore, stamp: stampBefore }, after: afterIdle }));

if (SHOT) {
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(SHOT, Buffer.from(shot.result.data, 'base64'));
  console.log('  截图已保存:', SHOT);
}

const failed = results.filter(r => !r.ok).length;
console.log(`\n通过 ${results.length - failed} 项，失败 ${failed} 项`);
ws.close();
process.exit(failed ? 1 : 0);

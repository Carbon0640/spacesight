/**
 * SpaceSight 文档截图工具（CDP）。
 *
 * 为什么不用 `msedge --screenshot`：当机器上已经有浏览器实例在跑时，
 * 命令行截图可能抓到"用户当前那一个窗口"，把真实数据截进去。
 * 这里连接 DevTools，**先断言页面确实是预期的演示实例**（端口 / 目标数 / 磁盘演示值 /
 * 当前视图 / 内容已渲染），断言不通过就拒绝写文件。
 *
 * 用法：
 *   node tools/shoot.mjs <baseUrl> <outDir> <期望端口> <期望目标数> <view...>
 * 例：
 *   CDP_PORT=9334 node tools/shoot.mjs "http://127.0.0.1:8791/?demo=1" docs/img 8791 5 tree treemap apps
 */
import fs from 'node:fs';
import path from 'node:path';

const [baseUrl, outDir, expectPort, expectRoots, ...views] = process.argv.slice(2);
if (!baseUrl || !outDir || !expectPort || !expectRoots || !views.length) {
  console.error('用法: node tools/shoot.mjs <baseUrl> <outDir> <端口> <目标数> <view...>');
  process.exit(2);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const CDP = process.env.CDP_PORT || 9333;

const list = await (await fetch(`http://127.0.0.1:${CDP}/json/list`)).json();
const page = list.find(t => t.type === 'page');
if (!page) throw new Error(`CDP 上没有页面目标，请用 --remote-debugging-port=${CDP} 启动浏览器`);
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
async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  const ex = r.result?.exceptionDetails;
  if (ex) throw new Error(ex.exception?.description || ex.text);
  return r.result?.result?.value;
}

await send('Runtime.enable');
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride',
  { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
fs.mkdirSync(outDir, { recursive: true });

// 只加载一次页面，之后用 switchView 切换（避免 hash-only 导航不触发重新加载）
console.log(`\n[load] ${baseUrl}`);
await send('Page.navigate', { url: baseUrl + '#tree' });
let booted = false;
for (let i = 0; i < 60; i++) {
  booted = await evaluate(`typeof S === 'object' && S.booted === true && S.roots.length > 0`);
  if (booted) break;
  await sleep(250);
}
if (!booted) { console.error('  ✗ 页面未完成启动'); process.exit(1); }

const readyExpr = {
  tree: `document.querySelectorAll('#tree .node').length > 0`,
  treemap: `document.querySelectorAll('#treemap .tm-cell').length > 0`,
  apps: `document.querySelectorAll('#apps-list .app-row').length > 0`,
};

let failed = 0;
for (const view of views) {
  await evaluate(`switchView(${JSON.stringify(view)})`);
  let ready = false;
  for (let i = 0; i < 40; i++) {
    ready = await evaluate(`S.view === ${JSON.stringify(view)} && (${readyExpr[view] || 'true'})`);
    if (ready) break;
    await sleep(250);
  }
  const state = await evaluate(`({
    port: location.port,
    roots: S.roots.length,
    view: S.view,
    disk: (document.getElementById('metric-disk') || {}).textContent || '',
    cells: document.querySelectorAll('#treemap .tm-cell').length,
    apps: document.querySelectorAll('#apps-list .app-row').length,
    crumbs: (document.getElementById('tm-crumb') || {}).textContent || '',
  })`);

  const problems = [];
  if (!ready) problems.push(`内容未渲染完成（${view}）`);
  if (String(state.port) !== String(expectPort)) problems.push(`端口 ${state.port} ≠ ${expectPort}`);
  if (Number(state.roots) !== Number(expectRoots)) problems.push(`目标数 ${state.roots} ≠ ${expectRoots}`);
  if (state.view !== view) problems.push(`视图 ${state.view} ≠ ${view}`);
  if (!state.disk.includes('2.00 TB')) problems.push(`磁盘计量未进入演示模式：${state.disk}`);

  if (problems.length) {
    console.error(`  ✗ [${view}] 拒绝截图：` + problems.join('；'));
    failed++;
    continue;
  }
  await sleep(700);   // 让入场动画收尾
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  const name = view === 'tree' ? '01-tree.png' : view === 'treemap' ? '02-treemap.png' : '03-apps.png';
  const out = path.join(outDir, name);
  fs.writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
  console.log(`  ✓ [${view}] 端口 ${state.port} / 目标 ${state.roots} / 方块 ${state.cells} / 台账 ${state.apps} → ${name}`);
  console.log(`        面包屑: ${state.crumbs.trim().slice(0, 70)}`);
}
ws.close();
process.exit(failed ? 1 : 0);

// 页面 JS 冒烟测试:模拟浏览器环境,把 panel 页面的脚本真正跑一遍。
// 覆盖:顶层初始化、一次成功 poll+render、三次失败 poll(离线路径)、令牌为空路径。
const vm = require('vm');
const fs = require('fs');

const _els = {};
function mkEl(id) {
  return { id, style: {}, dataset: {}, value: '', textContent: '', innerHTML: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, appendChild() {}, focus() {}, click() {},
    scrollTop: 0, scrollHeight: 100, clientHeight: 50, querySelectorAll: () => [] };
}
global.document = {
  getElementById: id => (_els[id] ??= mkEl(id)),
  querySelectorAll: () => [], addEventListener() {},
};
global.location = { search: '', pathname: '/', hash: '' };
global.history = { replaceState() {} };
global.localStorage = { _s: {},
  getItem(k) { return this._s[k] ?? null; },
  setItem(k, v) { this._s[k] = v; }, removeItem(k) { delete this._s[k]; } };
global.prompt = () => 'stub-token';
global.alert = m => console.log('  [alert]', m);
global.setInterval = () => 0;           // 不进入轮询循环,手动控制
global.clearTimeout = () => {};

const SNAP = { now: '21:40:00', task: null, queue: [], approval: null,
  session: '默认', sessions: ['默认', '项目A'], models: ['glm-5.3-flash'],
  modes: { default: '默认', acceptEdits: '自动编辑', bypass: '全自动' },
  model: 'glm-5.3-flash', mode: 'default', recent: [], replies: [],
  trace: [
    { k: 'think', text: '用户想要…我先看看目录结构', u: { in: 12000, out: 356, cache: 66000 } },
    { k: 'tool', name: 'Bash', text: 'ls -la D:/研', u: null },
    { k: 'tool', name: 'Read', text: 'D:\\研\\项目A\\初稿.docx（这里是很长的路径加多行\n第二行内容）', u: null },
    { k: 'ask', text: 'Bash: python analyze.py', u: null },
    { k: 'text', text: '分析结果如下…', u: { in: 15000, out: 800, cache: 70000 } },
  ],
  usage: { in: 27000, out: 1156, cache: 136000, turns: 2, cost: 0.0421 },
  usage_all: { in: 127000, out: 91156, cache: 536000, turns: 23, cost: 0.421, tasks: 7 },
  dingtalk_ok: true };

let failMode = false;
global.fetch = async () => {
  if (failMode) throw new Error('network down');
  return { status: 200, ok: true, json: async () => SNAP };
};

(async () => {
  let ok = true;
  try {
    vm.runInThisContext(fs.readFileSync('_page_check.js', 'utf-8'),
                        { filename: 'page.js' });
    console.log('1. 顶层初始化(含首次 poll+render) …… 通过');
  } catch (e) { console.log('1. 顶层初始化 …… 失败:', e.message); ok = false; }

  try { await poll(); console.log('2. 成功路径 poll+render …… 通过'); }
  catch (e) { console.log('2. 成功路径 …… 失败:', e.message); ok = false; }

  try { failMode = true; await poll(); await poll(); await poll();
        const feed = document.getElementById('feed').innerHTML;
        if (feed.includes('连不上电脑')) console.log('3. 离线路径(3 次失败出提示) …… 通过');
        else { console.log('3. 离线路径 …… 失败: 没出现离线提示, feed=', feed.slice(0, 60)); ok = false; } }
  catch (e) { console.log('3. 离线路径 …… 失败(异常冒出):', e.message); ok = false; }

  // 抽屉:三个 sheet 函数要能跑
  try { sheetSession(); sheetModel(); sheetMode(); sheetUsage(); closeSheet();
        console.log('4. 会话/模型/模式/消耗明细抽屉 …… 通过'); }
  catch (e) { console.log('4. 抽屉 …… 失败:', e.message); ok = false; }

  // 思维链:render 后 feed 里应有思考块/工具行/审批行/token 章数
  try { failMode = false; await poll();   // 恢复成功路径,重新渲染
        const feed = document.getElementById('feed').innerHTML;
        const need = ['class="thk', 'class="tlk', 'askline', 'class="tk"', '思考'];
        const miss = need.filter(s => !feed.includes(s));
        if (miss.length) { console.log('5. 思维链渲染 …… 失败: 缺少', miss.join(',')); ok = false; }
        else console.log('5. 思维链渲染(思考/工具/审批/章数) …… 通过'); }
  catch (e) { console.log('5. 思维链渲染 …… 失败:', e.message); ok = false; }

  // 徽章:消耗徽章要出现(任务快照里 usage_all 有值)
  try { const b = document.getElementById('badges').innerHTML;
        if (!b.includes('🪙')) { console.log('6. token 徽章 …… 失败: 没有 🪙 徽章'); ok = false; }
        else console.log('6. token 徽章 …… 通过'); }
  catch (e) { console.log('6. token 徽章 …… 失败:', e.message); ok = false; }

  console.log(ok ? 'SMOKE_OK' : 'SMOKE_FAIL');
  process.exit(ok ? 0 : 1);
})();

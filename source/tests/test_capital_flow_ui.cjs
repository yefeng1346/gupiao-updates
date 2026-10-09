const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(match => match[1]);
assert.ok(scripts.length > 0);
scripts.forEach(script => new vm.Script(script));

function extract(name, next) {
  const start = html.indexOf(`function ${name}(`);
  const end = html.indexOf(`function ${next}(`, start);
  assert.ok(start > 0 && end > start);
  // Include `async` when testing an asynchronous page function.
  return (['refreshCapitalFlow', 'loadCurrentFlow', 'clearCapitalFlowData', 'downloadFlowHistory'].includes(name) ? 'async ' : '') + html.slice(start, end).replace(/\s+async\s*$/, '');
}

const elements = new Map();
for (const id of ['capitalFlowStatus', 'capitalFlowTable', 'refreshCapitalFlowBtn', 'refreshCapitalFlowOnlineBtn', 'retryCapitalFlowBtn', 'capitalFlowLimit', 'capitalFlowDate', 'capitalFlowSource', 'capitalFlowWindowDays', 'capitalFlowMinInflowDays', 'cancelFlowHistoryBtn', 'flowImportBtn', 'capitalFlowImportPanel']) {
  elements.set(id, {innerHTML: '', textContent: '', disabled: false, value: ''});
}
elements.get('capitalFlowLimit').value = '50';
elements.get('capitalFlowDate').value = '2025-01-17';
elements.get('capitalFlowSource').value = 'eastmoney';
elements.get('capitalFlowWindowDays').value = '10';
elements.get('capitalFlowMinInflowDays').value = '6';
const stored = new Map();
const calls = [];
let cacheRestores = 0;
for (const id of ['currentFlowRefresh', 'currentFlowLimit', 'currentFlowTable', 'currentFlowStatus', 'clearCapitalFlowBtn', 'confirmClearCapitalFlowBtn', 'cancelClearCapitalFlowBtn', 'clearCapitalFlowConfirm']) {
  elements.set(id, {innerHTML: '', textContent: '', disabled: false, value: '50'});
}
const context = vm.createContext({
  $: id => elements.get(id),
  typeLabel: () => '概念板块',
  quoteChange: () => '-',
  selectedSectorTypes: () => ['concept'],
  localStorage: {setItem(key,value) {stored.set(key,String(value));}, getItem(key) {return stored.get(key) ?? null;}, removeItem(key) {stored.delete(key);}},
  localDateInputValue: () => '2026-10-06',
  restoreCapitalFlow: async () => { cacheRestores++; },
  api: async url => { calls.push(url); throw new Error('历史接口连接失败'); },
  setTimeout: fn => fn(),
  URLSearchParams,
});
vm.runInContext([
  extract('esc', 'cleanReportInline'),
  extract('flowAmount', 'flowRatio'),
  extract('flowRatio', 'localDateInputValue'),
  extract('renderCapitalFlowPanel', 'renderCapitalFlowOutcomes'),
  extract('renderCapitalFlowOutcomes', 'refreshCapitalFlow'),
  extract('downloadFlowHistory', 'importFlowFile'),
  extract('refreshCapitalFlow', 'restoreCapitalFlow'),
  extract('loadCurrentFlow', 'renderCapitalFlowPanel'),
  extract('clearCapitalFlowData', 'loadCurrentFlow'),
].join('\n'), context);

const partial = {
  sector_type: 'concept', requested_date: '2025-01-17', report_date: '2025-01-08',
  window_dates: ['2025-01-06', '2025-01-07', '2025-01-08'], partial: true,
  rows: [], inflow_days_rank: [], warnings: ['<script>unsafe</script>'],
  history_coverage: {complete_boards: 0, requested_boards: 2, window_days: 3},
};
const partialHtml = context.renderCapitalFlowPanel(partial);
assert.ok(partialHtml.includes('暂不能判断'));
assert.ok(partialHtml.includes('数据未完整'));
assert.ok(partialHtml.includes('所选日期 2025-01-17'));
assert.ok(partialHtml.includes('2025-01-08'));
assert.ok(partialHtml.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
assert.ok(!partialHtml.includes('<script>unsafe</script>'));
const completeHtml = context.renderCapitalFlowPanel({
  ...partial, warnings: [], partial: false,
  history_coverage: {complete_boards: 2, requested_boards: 2, window_days: 10},
});
assert.ok(completeHtml.includes('10日数据完整，暂无符合'));
assert.ok(html.includes('<summary><strong>高级：历史补缺、来源文件、删除历史档案</strong></summary>'));
const customHtml = context.renderCapitalFlowPanel({...partial,window_days:5,min_inflow_days:3,window_complete:true,
  history_coverage:{complete_boards:1,requested_boards:1,window_days:5},
  inflow_days_rank:[{sector_name:'<custom>',positive_flow_days:3,window_main_net_inflow:1000000,latest_main_net_inflow:1}]});
assert.match(customHtml,/5天内至少3天资金流入榜/);
assert.match(customHtml,/3\/5 天/);
assert.match(customHtml,/5日主力净额/);
assert.ok(!customHtml.includes('10日'));
assert.match(customHtml,/&lt;custom&gt;/);
context.initializeFlowRule();
assert.equal(elements.get('capitalFlowWindowDays').value,10);
stored.set('gupiao.capitalFlowRule','{"window_days":5,"min_inflow_days":3}');
context.initializeFlowRule();
assert.equal(elements.get('capitalFlowWindowDays').value,5);
assert.equal(elements.get('capitalFlowMinInflowDays').value,3);
stored.set('gupiao.capitalFlowRule','invalid');
context.initializeFlowRule();
assert.equal(elements.get('capitalFlowWindowDays').value,10);

async function main() {
  const table = elements.get('capitalFlowTable');
  table.innerHTML = '<div>上一次的结果</div>';
  context.renderCapitalFlowOutcomes([{type: 'concept', error: new Error('连接失败')}], '刷新');
  assert.equal(table.innerHTML, '<div>上一次的结果</div>');
  await context.refreshCapitalFlow(false, true);
  assert.equal(calls[0], '/api/sector-capital-flow/history/jobs');
  assert.ok(!calls[0].includes('refresh=true'));
  assert.equal(table.innerHTML, '<div>上一次的结果</div>');
  for (const id of ['refreshCapitalFlowBtn', 'refreshCapitalFlowOnlineBtn', 'retryCapitalFlowBtn']) {
    assert.equal(elements.get(id).disabled, false);
  }
  assert.match(elements.get('capitalFlowStatus').innerHTML, /历史接口连接失败/);
  calls.length = 0;
  await context.refreshCapitalFlow(true);
  assert.equal(calls[0], '/api/sector-capital-flow/history/jobs');
  assert.ok(calls[1].startsWith('/api/sector-capital-flow/current/cache'));
  assert.equal(table.innerHTML, '<div>上一次的结果</div>');
  context.api = async (url, options) => {
    if (url.endsWith('/history/jobs')) {
      assert.equal(options.method, 'POST');
      const body = JSON.parse(options.body);
      assert.equal(body.window_days,10);
      assert.equal(body.min_inflow_days,6);
      return {id:'test', status:'running'};
    }
    return {id:'test', status:'partial', result:partial, attempted:2, total:2, downloaded:1, failed:1};
  };
  await context.downloadFlowHistory('2025-01-17', 50);
  assert.match(elements.get('capitalFlowStatus').innerHTML, /不代表完整下载成功/);
  table.innerHTML = '<div>上一次的结果</div>';
  context.api = async url => {
    assert.ok(url.startsWith('/api/sector-capital-flow/current'));
    return {rows: [{sector_name: '<unsafe>', sector_code: 'BK1', rank: 1}], total: 504, source: 'current', updated_at: 'now', closed_rows_saved: 504, confirmed_trade_date: '2026-09-30'};
  };
  await context.loadCurrentFlow(true);
  assert.ok(elements.get('currentFlowTable').innerHTML.includes('504'));
  assert.ok(elements.get('currentFlowTable').innerHTML.includes('&lt;unsafe&gt;'));
  assert.equal(cacheRestores, 1);
  assert.ok(elements.get('currentFlowTable').innerHTML.includes('2026-09-30'));
  assert.equal(table.innerHTML, '<div>上一次的结果</div>');
  const saved = elements.get('currentFlowTable').innerHTML;
  context.api = async () => { throw new Error('offline'); };
  await context.loadCurrentFlow(true);
  assert.equal(elements.get('currentFlowTable').innerHTML, saved);
  assert.equal(elements.get('currentFlowRefresh').disabled, false);
  await context.clearCapitalFlowData();
  assert.equal(elements.get('currentFlowTable').innerHTML, saved);
  context.api = async (url, options) => {
    assert.equal(url, '/api/sector-capital-flow/clear');
    assert.equal(options.method, 'POST');
    return {deleted_rows: 123, backup_path: 'backup.json'};
  };
  await context.clearCapitalFlowData();
  assert.equal(elements.get('capitalFlowTable').innerHTML, '');
  assert.equal(elements.get('currentFlowTable').innerHTML, '');
  assert.ok(elements.get('currentFlowStatus').textContent.includes('123'));
  assert.equal(elements.get('clearCapitalFlowConfirm').hidden, true);
  elements.get('capitalFlowWindowDays').value = '5';
  elements.get('capitalFlowMinInflowDays').value = '3';
  context.api = async url => {
    assert.match(url,/window_days=5&min_inflow_days=3/);
    return {...partial,window_days:5,min_inflow_days:3};
  };
  await context.refreshCapitalFlow();
  assert.match(table.innerHTML,/5天内至少3天资金流入榜/);
  const restoresBefore = cacheRestores;
  elements.get('capitalFlowWindowDays').value = '2';
  await context.changeFlowRule({target:{id:'capitalFlowWindowDays'}});
  assert.equal(elements.get('capitalFlowMinInflowDays').value,2);
  assert.equal(elements.get('capitalFlowMinInflowDays').max,2);
  assert.equal(cacheRestores,restoresBefore+1);
  assert.equal(JSON.parse(stored.get('gupiao.capitalFlowRule')).window_days,2);
  elements.get('capitalFlowMinInflowDays').value = '3';
  await context.changeFlowRule({target:{id:'capitalFlowMinInflowDays'}});
  assert.match(elements.get('capitalFlowStatus').textContent,/整数/);
  assert.equal(cacheRestores,restoresBefore+1);
  console.log('PASS: script syntax, incomplete/empty distinction, escaping, failed refresh preservation, retry-only request, button reset');
}
main().catch(error => { console.error(error); process.exitCode = 1; });

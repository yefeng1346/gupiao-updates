const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname,'../web/index.html'),'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const extract = (name,next) => html.slice(html.indexOf(`function ${name}(`),html.indexOf(`function ${next}(`,html.indexOf(`function ${name}(`)));
const context = vm.createContext({});
vm.runInContext([extract('esc','cleanReportInline'),extract('miniTable','renderHistory'),
  extract('flowAmount','flowRatio'),extract('threeDayFlowCell','localDateInputValue')].join('\n'),context);
assert.match(context.threeDayFlowCell({three_day_flow_status:'complete',three_day_main_net_inflow:1e8}),/\+1.00亿/);
assert.match(context.threeDayFlowCell({three_day_flow_status:'complete',three_day_main_net_inflow:-1e4}),/-1.00万/);
assert.match(context.threeDayFlowCell({three_day_flow_status:'complete',three_day_main_net_inflow:0}),/0元/);
assert.match(context.threeDayFlowCell({three_day_flow_status:'missing',three_day_flow_available_days:2}),/数据不足（2\/3天）/);
assert.match(context.threeDayFlowCell({three_day_flow_status:'unmatched'}),/未匹配/);
const reference = context.threeDayFlowCell({three_day_flow_status:'complete',three_day_main_net_inflow:100,
  three_day_flow_match:'unique_name',three_day_flow_code:'<unsafe>'});
assert.match(reference,/同名板块参考/); assert.match(reference,/&lt;unsafe&gt;/);
const table = context.miniTable([{sector_code:'880729',sector_name:'人形机器人',three_day_main_net_inflow:70,three_day_flow_status:'complete'}],
  [{key:'three_day_main_net_inflow',label:'3日资金净流入',render:(_,row)=>context.threeDayFlowCell(row)}]);
assert.match(table,/3日资金净流入/); assert.match(table,/\+70元/); assert.match(table,/selectable-row/);
assert.equal((html.match(/key:"three_day_main_net_inflow",label:"3日资金净流入"/g)||[]).length,2);
assert.ok(!html.includes('loadSectorLeaders(report, tenDayRows'));
assert.ok(!html.includes('loadDualSectorLeaders(panelKey, report, tenRows'));
assert.ok(html.includes('loadSectorLeaders(report, fiveTop100Rows'));
assert.match(context.threeDayFlowStatus({three_day_flow:{window_dates:['2026-09-28','2026-09-29','2026-09-30'],complete_boards:1,total_boards:2}}),/2026-09-28 至 2026-09-30.*1\/2/);
console.log('PASS: single/dual column, positive/negative/zero amounts, missing coverage, reference labels, no module7 leader requests');

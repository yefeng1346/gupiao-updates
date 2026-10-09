const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname,'../web/index.html'),'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const extract = (name,next) => html.slice(html.indexOf(`function ${name}(`),html.indexOf(`function ${next}(`,html.indexOf(`function ${name}(`)));
const context = vm.createContext({});
vm.runInContext([extract('esc','cleanReportInline'),extract('miniTable','renderHistory'),
  extract('flowAmount','flowRatio'),extract('fiveDayFlowCell','localDateInputValue')].join('\n'),context);
assert.match(context.fiveDayFlowCell({five_day_flow_status:'complete',five_day_main_net_inflow:1e8}),/\+1.00亿/);
assert.match(context.fiveDayFlowCell({five_day_flow_status:'complete',five_day_main_net_inflow:-1e4}),/-1.00万/);
assert.match(context.fiveDayFlowCell({five_day_flow_status:'complete',five_day_main_net_inflow:0}),/0元/);
assert.match(context.fiveDayFlowCell({five_day_flow_status:'missing',five_day_flow_available_days:4}),/数据不足（4\/5天）/);
assert.match(context.fiveDayFlowCell({five_day_flow_status:'unmatched'}),/未匹配/);
const reference = context.fiveDayFlowCell({five_day_flow_status:'complete',five_day_main_net_inflow:100,
  five_day_flow_match:'unique_name',five_day_flow_code:'<unsafe>'});
assert.match(reference,/同名板块参考/); assert.match(reference,/&lt;unsafe&gt;/);
const table = context.miniTable([{sector_code:'880729',sector_name:'人形机器人',five_day_main_net_inflow:70,five_day_flow_status:'complete'}],
  [{key:'five_day_main_net_inflow',label:'5日资金净流入',render:(_,row)=>context.fiveDayFlowCell(row)}]);
assert.match(table,/5日资金净流入/); assert.match(table,/\+70元/); assert.match(table,/selectable-row/);
assert.equal((html.match(/key:"five_day_main_net_inflow",label:"5日资金净流入"/g)||[]).length,2);
assert.ok(!html.includes('loadSectorLeaders(report, tenDayRows'));
assert.ok(!html.includes('loadDualSectorLeaders(panelKey, report, tenRows'));
assert.ok(html.includes('loadSectorLeaders(report, fiveTop100Rows'));
assert.match(context.fiveDayFlowStatus({five_day_flow:{window_dates:['2026-09-23','2026-09-24','2026-09-28','2026-09-29','2026-09-30'],complete_boards:1,total_boards:2}}),/2026-09-23 至 2026-09-30.*1\/2/);
console.log('PASS: single/dual column, positive/negative/zero amounts, missing coverage, reference labels, no module7 leader requests');

const fs=require('fs'),assert=require('assert'),vm=require('vm');
const html=fs.readFileSync('web/index.html','utf8');
for(const id of ['currentFlowLimit','capitalFlowLimit']){
  const select=html.match(new RegExp(`<select id="${id}">([\\s\\S]*?)</select>`))[1];
  assert.match(select,/<option value="10" selected>/);
  assert.equal((select.match(/selected/g)||[]).length,1);
  assert.match(select,/<option value="1000">/);
}
for(const removed of ['importSnapshotBtn','snapshotFile','snapshotJson','parseSnapshotCsv']) assert.ok(!html.includes(removed));
assert.match(html,/id="candidateAmount"[^>]*value="1000"/);
for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
console.log('PASS: top10 defaults, optional larger lists, removed snapshot bindings, lower liquidity default and script syntax');

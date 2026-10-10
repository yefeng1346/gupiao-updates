await page.reload({waitUntil:'domcontentloaded'});
await expect(page.locator('#candidateResults tbody')).toContainText('依据：板块指标');
assert.equal(globalThis.candidatePageErrors.length,0);
const count=await page.fetch('/test/candidate-metrics',{as:'json'});
assert.equal(count.json.model_calls,1);
await page.close();
return {pageErrors:[],modelCalls:count.json.model_calls,verified:true};

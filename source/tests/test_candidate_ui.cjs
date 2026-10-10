const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync('web/candidates.js','utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function setup(api) {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id,{value:'',checked:false,disabled:false,hidden:false,textContent:'',innerHTML:'',querySelectorAll:()=>[]});
    return nodes.get(id);
  };
  for (const [id,value] of Object.entries({candidateLimit:'10',candidateAmount:'2000',formulaTimeframe:'daily',formulaSelect:'1'})) $(id).value=value;
  $('candidateUseAI').checked=true;
  const esc = text => String(text ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  vm.runInNewContext(code,{$,api,esc,providerLabel:v=>v,typeLabel:v=>v,selectedProvider:()=> 'akshare',selectedLlmModel:()=> 'test-model',document:{querySelectorAll:()=>[]},localStorage:{getItem:()=>null,setItem:()=>{}},setTimeout:fn=>setImmediate(fn)});
  return $;
}
const row={name:'<img src=x onerror=alert()>',code:'600001',score:90,close:10,return_5d:2,boards:[],reasons:['趋势'],risks:['风险'],data_date:'2026-10-09',evidence:{trend:'事实'},ai:{status:'completed',model:'mock',cached:true,content:{reasons:[{text:'<script>oops</script>',evidence_ids:['trend']}],risks:[],observe:[]}}};
const completed={id:'safe-id',status:'completed',message:'完成',result:{report_date:'2026-10-09',provider:'akshare',sector_type:'concept',rows:[row],disclaimer:'风险',rules:'规则',warnings:[]}};
(async()=>{
  const calls=[];
  const $=setup(async(url,options)=>{calls.push([url,options]);return {run:completed};});
  await flush();
  assert.equal(calls.length,1);
  assert.equal(calls[0][1],undefined,'restore must never POST');
  assert.match($('candidateResults').innerHTML,/&lt;img/);
  assert.match($('candidateResults').innerHTML,/&lt;script/);
  assert.doesNotMatch($('candidateResults').innerHTML,/<script>/);
  const original=$('candidateResults').innerHTML;
  // Failed generation preserves previous data and never cancels the old run.
  calls.length=0;
  const failing=setup(async(url,options)=>{calls.push([url,options]);if(!options)return {run:completed};throw Error('offline');});
  await flush();
  await failing('candidateGenerate').onclick();
  assert.equal(failing('candidateResults').innerHTML,original);
  assert.match(failing('candidateStatus').textContent,/offline/);
  assert.equal(calls.filter(c=>c[1]?.method==='POST').length,1);
  assert.equal(failing('candidateGenerate').disabled,false);
  const sequence=[];
  const live=setup(async(url,options)=>{
    sequence.push(url);
    if(url.includes('latest'))return {run:null};
    if(options)return {...completed,status:'interpreting',message:'候选先显示',result:{...completed.result,rows:[{...row,ai:{status:'loading'}}]}};
    assert.match(live('candidateResults').innerHTML,/正在生成解读/);
    return completed;
  });
  await flush();
  await live('candidateGenerate').onclick();
  assert.equal(sequence.length,3);
  assert.match(live('candidateResults').innerHTML,/本地缓存/);
  assert.equal(live('candidateCancel').hidden,true);
  console.log('candidate UI: restore GET, escaped facts/AI, failure retention, progressive polling passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

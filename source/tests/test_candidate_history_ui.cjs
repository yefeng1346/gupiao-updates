const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map(),calls=[];
const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',innerHTML:'',textContent:'',disabled:false,addEventListener:(event,fn)=>{$(id).change=fn;}});return nodes.get(id);};
$('candidateProvider').value='akshare';$('candidateType').value='concept';$('candidateHistoryDays').value='5';
const data={report_date:'2026-09-30',provider:'akshare',sector_type:'concept',time_basis:'保存时间',saved_at:'2026-09-30',entry_date:'2026-10-08',exit_date:'2026-10-14',days:5,data_cutoff:'2026-10-30',note:'缺失不计零',
summary:{total:1,complete:0,pending:0,unavailable:1,up:0,down:0,flat:0,up_ratio_pct:null,average_return_pct:null},
rows:[{name:'<img onerror=oops>',code:'600001',score:80,message:'缺失',return_pct:null,drawdown_pct:null,ai:{status:'completed',content:{reasons:[{text:'<script>bad</script>'}]}},reasons:['原依据']}]};
let fail=false;
vm.runInNewContext(fs.readFileSync('web/candidate_history.js','utf8'),{$,esc:text=>String(text??'').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),providerLabel:v=>v,typeLabel:v=>v,
 api:async(url,options)=>{calls.push([url,options]);if(fail)throw Error('offline');return url.includes('/history?')?{runs:[{id:'one',saved_at:'2026-09-30T16:00:00',report_date:'2026-09-30',count:1}]}:data;}});
(async()=>{
 assert.equal(calls.length,0,'history must not auto-call generation or AI');
 await $('candidateHistoryLoad').onclick();
 assert.match($('candidateHistoryRun').innerHTML,/one/);
 $('candidateHistoryRun').value='one';await $('candidateHistoryEvaluate').onclick();
 const html=$('candidateHistoryResults').innerHTML;
 assert.match(html,/&lt;img/);assert.match(html,/&lt;script/);assert.doesNotMatch(html,/<script>/);
 assert.match(html,/上涨占比 —/);
 assert.equal(calls.every(([url,options])=>!options && !url.includes('/generate') && !url.includes('/interpret')),true);
 fail=true;await $('candidateHistoryEvaluate').onclick();assert.equal($('candidateHistoryResults').innerHTML,html);
 assert.equal($('candidateHistoryEvaluate').disabled,false);
 $('candidateProvider').change();assert.equal($('candidateHistoryResults').innerHTML,'');
 console.log('PASS: read-only history GETs, no AI, escaping, missing ratio, failure retention, scope reset');
})().catch(error=>{console.error(error);process.exitCode=1;});

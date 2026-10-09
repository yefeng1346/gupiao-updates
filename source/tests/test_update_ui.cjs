const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const apply='async '+html.slice(html.indexOf('function applyUpdate('),html.indexOf('function esc(')).replace(/\s+$/,'');
const escape=html.slice(html.indexOf('function esc('),html.indexOf('function cleanReportInline('));

async function scenario(states,installFailure=false){
  const ids=['applyUpdateBtn','updateNotice','updateProgress','cancelUpdateDownload','retrySoftwareUpdate','status'];
  const elements=new Map(ids.map(id=>[id,{textContent:'',innerHTML:'',disabled:false}]));
  const calls=[];
  const context=vm.createContext({$:id=>elements.get(id),setTimeout:fn=>fn(),api:async (url,options)=>{
    calls.push(url);
    if(url==='/api/update/apply'){assert.equal(options.method,'POST');return {status:'downloading'};}
    if(url==='/api/update/status'){
      const state=states.shift();assert.ok(state,'Unexpected extra poll');
      if(state.cancel){await elements.get('cancelUpdateDownload').onclick();}
      return state;
    }
    if(url==='/api/update/cancel')return {status:'cancelled'};
    if(url==='/api/update/install'){
      assert.equal(options.method,'POST');
      if(installFailure)throw new Error('还有其他窗口');
      return {status:'installing',message:'正在重启安装'};
    }
    throw new Error('Unexpected URL');
  }});
  vm.runInContext(apply+'\n'+escape,context);
  await context.applyUpdate();
  return {calls,elements};
}

(async()=>{
  const success=await scenario([{status:'downloading',bytes:1048576,total:2097152,attempt:1},{status:'ready',message:'校验完成'}]);
  assert.deepEqual(success.calls,['/api/update/apply','/api/update/status','/api/update/status','/api/update/install']);
  assert.match(success.elements.get('updateNotice').innerHTML,/正在重启安装/);
  const cancelled=await scenario([{status:'downloading',cancel:true},{status:'cancelled',message:'已取消'}]);
  assert.ok(cancelled.calls.includes('/api/update/cancel'));
  assert.ok(!cancelled.calls.includes('/api/update/install'));
  assert.match(cancelled.elements.get('updateNotice').innerHTML,/原版本可继续使用/);
  assert.equal(typeof cancelled.elements.get('retrySoftwareUpdate').onclick,'function');
  const failed=await scenario([{status:'failed',message:'<script>unsafe</script>'}]);
  assert.ok(!failed.calls.includes('/api/update/install'));
  assert.ok(failed.elements.get('updateNotice').innerHTML.includes('&lt;script&gt;'));
  const blocked=await scenario([{status:'ready',message:'ready'}],true);
  assert.match(blocked.elements.get('updateNotice').innerHTML,/还有其他窗口/);
  assert.equal(typeof blocked.elements.get('retrySoftwareUpdate').onclick,'function');
  console.log('PASS: update preparation/poll/install order, cancellation, download errors, escaping and multi-window retry');
})().catch(error=>{console.error(error);process.exitCode=1;});

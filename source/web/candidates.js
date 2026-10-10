/* One-click watchlist: facts arrive before AI; failures never erase facts. */
(() => {
  let currentRun = null, busy = false, generation = 0;
  const controls = ['candidateProvider','candidateType','candidateLimit','candidateAmount','candidateRequireFlow','candidateUseFormula','candidateUseAI'];
  const active = run => ['generating','interpreting'].includes(run.status);
  const setBusy = value => {
    busy = value;
    $('candidateGenerate').disabled = value;
    $('candidateGenerate').textContent = $('candidateUseAI').checked ? '生成候选股票＋AI解读' : '生成候选股票（不调用AI）';
    $('candidateCancel').hidden = !value;
    $('candidateCancel').disabled = !currentRun || !active(currentRun);
    controls.forEach(id => $(id).disabled = value);
    document.querySelectorAll('[data-candidate-retry]').forEach(button => button.disabled = value);
  };
  const displayNumber = value => value == null || !Number.isFinite(Number(value)) ? '缺失' : Number(value).toFixed(2);
  const evidenceLabel = key => ({board:'板块指标',trend:'个股趋势',liquidity:'成交与量比',flow:'板块资金',market:'市场概况',formula:'技术公式'}[key] || key);
  function aiCell(row) {
    const ai = row.ai || {};
    if (ai.status === 'completed') {
      return `<small>${ai.cached ? '本地缓存' : 'AI生成'} · ${esc(ai.model || '')} · 非交易指令</small>` +
        [['reasons','解读'],['risks','风险'],['observe','待观察']].map(([key,label]) => `<p><b>${label}</b></p><ul>${(ai.content?.[key] || []).map(item => `<li>${esc(item.text)}<small>依据：${esc((item.evidence_ids || []).map(evidenceLabel).join('、'))}</small></li>`).join('')}</ul>`).join('');
    }
    const text = ai.status === 'failed' ? ai.message : ai.status === 'disabled' ? '未调用AI' : ai.status === 'loading' ? '正在生成解读…' : '等待解读；取消后不会自动调用';
    return `<p class="muted">${esc(text)}</p>${['failed','disabled','pending'].includes(ai.status) ? `<button type="button" data-candidate-retry="${esc(row.code)}" ${busy ? 'disabled' : ''}>解读此股票／重试（可能收费）</button>` : ''}`;
  }
  function renderCandidateRun(run) {
    currentRun = run;
    $('candidateStatus').textContent = run.message || run.status;
    if (!run.result) return; // Preserve the previous successful result on failure.
    const result = run.result;
    const rows = result.rows || [];
    $('candidateResults').innerHTML = `<p><b>数据截至 ${esc(result.report_date)}</b> · ${esc(providerLabel(result.provider))} · ${esc(typeLabel(result.sector_type))} · ${rows.length}只候选</p>` +
      `<p class="notice">${esc(result.disclaimer)}</p><p class="muted">${esc(result.rules)}</p>` +
      `<details><summary>来源、缺失与剔除说明</summary><ul>${(result.warnings || []).map(w => `<li>${esc(w)}</li>`).join('')}</ul><p>${esc(JSON.stringify(result.skipped || {}))}</p></details>` +
      (rows.length ? `<div class="table-wrap"><table><thead><tr><th>候选股票</th><th>规则得分／证据</th><th>对应AI解读</th></tr></thead><tbody>${rows.map(row => `<tr><td><b>${esc(row.name)}</b><small>${esc(row.code)}</small><p>${(row.boards || []).map(b => esc(b.name)).join('、')}</p><p>收盘 ${displayNumber(row.close)}<br/>近5日涨幅 ${displayNumber(row.return_5d)}%</p></td><td><b>${esc(row.score)}/100（非上涨概率）</b><ul>${(row.reasons || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul><ul class="muted">${(row.risks || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul><details><summary>查看实际数据及日期</summary><p>${esc(row.data_date)}</p><pre style="white-space:pre-wrap">${esc(JSON.stringify(row.evidence, null, 2))}</pre></details></td><td style="min-width:260px;max-width:500px">${aiCell(row)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">当前数据和条件没有产生候选，详见来源与剔除说明；这不代表全市场没有符合条件的股票。</p>');
    $('candidateResults').querySelectorAll('[data-candidate-retry]').forEach(button => button.onclick = () => retryCandidate(button.dataset.candidateRetry));
  }
  async function poll(run, token) {
    while (token === generation) {
      renderCandidateRun(run);
      setBusy(active(run));
      if (!active(run)) return;
      await new Promise(resolve => setTimeout(resolve, 1200));
      if (token !== generation) return;
      run = await api(`/api/candidates/runs/${encodeURIComponent(run.id)}`);
    }
  }
  async function generateCandidateRun() {
    if (busy) return;
    const token = ++generation;
    currentRun = null;
    setBusy(true);
    $('candidateStatus').textContent = '正在启动候选筛选…';
    try {
      const body = {provider:$('candidateProvider').value,sector_type:$('candidateType').value,max_results:Number($('candidateLimit').value),min_amount:Number($('candidateAmount').value)*10000,require_flow:$('candidateRequireFlow').checked,ai_enabled:$('candidateUseAI').checked,model:selectedLlmModel()};
      if (!Number.isInteger(body.max_results) || body.max_results < 1 || body.max_results > 10) throw new Error('候选数量请填写1～10的整数');
      if (!Number.isFinite(body.min_amount) || body.min_amount < 1000000 || body.min_amount > 1000000000) throw new Error('最低日成交额请填写100～100000万元');
      if ($('candidateUseFormula').checked) {
        const timeframe = $('formulaTimeframe').value;
        if (!['daily','weekly'].includes(timeframe)) throw new Error('综合候选仅支持日线或周线技术公式，请先调整公式选股周期');
        const id = Number($('formulaSelect').value);
        if (!id) throw new Error('请先保存并选择一个技术公式');
        body.formula_id = id;
        body.timeframe = timeframe;
      }
      await poll(await api('/api/candidates/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),token);
    } catch (error) { if (token === generation) $('candidateStatus').textContent = `任务暂不可用：${error.message}；已显示结果保留，重开可恢复任务状态`; }
    finally { if (token === generation) setBusy(false); }
  }
  async function retryCandidate(code) {
    if (busy || !currentRun) return;
    const token = ++generation;
    setBusy(true);
    try {
      const run = await api(`/api/candidates/runs/${encodeURIComponent(currentRun.id)}/interpret`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,model:selectedLlmModel()})});
      await poll(run,token);
    } catch (error) { if (token === generation) $('candidateStatus').textContent = `解读未完成：${error.message}；候选结果保留`; }
    finally { if (token === generation) setBusy(false); }
  }
  async function restoreCandidates() {
    if (busy) return;
    const token = ++generation;
    try {
      const value = await api(`/api/candidates/latest?provider=${encodeURIComponent($('candidateProvider').value)}&sector_type=${encodeURIComponent($('candidateType').value)}`);
      if (token !== generation) return;
      if (value.run) { await poll(value.run,token); }
      else { currentRun=null; $('candidateResults').innerHTML=''; $('candidateStatus').textContent='此来源尚无候选记录；点击生成，默认解读前10只会调用现有模型'; }
    } catch (error) { if (token === generation) $('candidateStatus').textContent=`本地候选恢复失败：${error.message}`; }
    finally { if (token === generation) setBusy(false); }
  }
  $('candidateGenerate').onclick = generateCandidateRun;
  $('candidateUseAI').onchange = () => setBusy(false);
  $('candidateCancel').onclick = async () => {
    if (!currentRun) return;
    $('candidateCancel').disabled=true;
    try { await api(`/api/candidates/runs/${encodeURIComponent(currentRun.id)}/cancel`,{method:'POST'}); $('candidateStatus').textContent='已请求取消后续工作；正在执行的模型请求可能仍产生费用'; }
    catch(error) { $('candidateStatus').textContent=error.message; }
    finally { $('candidateCancel').disabled=false; }
  };
  $('candidateProvider').value = localStorage.getItem('gupiao.candidateProvider') || selectedProvider();
  if (!$('candidateProvider').value) $('candidateProvider').value='akshare';
  $('candidateType').value = localStorage.getItem('gupiao.candidateType') || 'concept';
  for (const id of ['candidateProvider','candidateType']) $(id).onchange = () => { localStorage.setItem(`gupiao.${id}`, $(id).value); void restoreCandidates(); };
  void restoreCandidates(); // Local GET only; never initiates a paid model request.
})();

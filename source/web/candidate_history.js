/* Read-only saved watchlist validation: no generation, retry, or model calls. */
(() => {
  let serial=0;
  const num=value=>value==null?'—':Number(value).toFixed(2);
  const controls=['candidateHistoryLoad','candidateHistoryRun','candidateHistoryDays','candidateHistoryEvaluate'];
  const busy=value=>controls.forEach(id=>$(id).disabled=value);
  const scope=()=>`provider=${encodeURIComponent($('candidateProvider').value)}&sector_type=${encodeURIComponent($('candidateType').value)}`;
  async function loadHistory() {
    const token=++serial; busy(true);
    $('candidateHistoryStatus').textContent='正在读取本机保存的候选名单…';
    try {
      const day=$('candidateHistoryDate').value;
      const data=await api(`/api/candidates/history?${scope()}${day?`&saved_date=${encodeURIComponent(day)}`:''}`);
      if(token!==serial)return;
      $('candidateHistoryRun').innerHTML='<option value="">请选择一次保存记录</option>'+data.runs.map(run=>`<option value="${esc(run.id)}">${esc(run.saved_at.replace('T',' '))} · 报告${esc(run.report_date)} · ${run.count}只</option>`).join('');
      $('candidateHistoryResults').innerHTML='';
      $('candidateHistoryStatus').textContent=data.runs.length?`找到${data.runs.length}次保存记录；同日多次分别保留，请选择一条验证。`:'该来源、范围和保存日期没有候选记录；不会重新选股或调用AI。';
    } catch(error) {if(token===serial)$('candidateHistoryStatus').textContent=`记录查询失败：${error.message}；原显示结果保留`;} 
    finally {if(token===serial)busy(false);}
  }
  function render(data) {
    const s=data.summary;
    $('candidateHistoryResults').innerHTML=`<p><b>原报告日 ${esc(data.report_date)}</b> · ${esc(providerLabel(data.provider))} · ${esc(typeLabel(data.sector_type))}<br/>${esc(data.time_basis)}：${esc(data.saved_at)}</p>`+
      `<p>固定观察窗口：${esc(data.entry_date||'日历未覆盖')} 开盘 → ${esc(data.exit_date||'日历未覆盖')} 收盘，${data.days}个交易日；行情截止 ${esc(data.data_cutoff)}</p><p class="notice">${esc(data.note)}</p>`+
      `<p>原名单${s.total}只 · 可统计${s.complete}只 · 观察期未满${s.pending}只 · 缺失/停牌等${s.unavailable}只<br/>上涨${s.up}只／下跌${s.down}只／持平${s.flat}只；上涨占比 ${num(s.up_ratio_pct)}${s.up_ratio_pct==null?'':'%'}；等权平均价格涨跌 ${num(s.average_return_pct)}${s.average_return_pct==null?'':'%'}（仅统计完整样本）</p>`+
      `<div class="table-wrap"><table><thead><tr><th>原候选股票／得分</th><th>起点开盘／终点收盘</th><th>价格涨跌</th><th>收盘序列最大回撤</th><th>数据状态及原解读</th></tr></thead><tbody>${data.rows.map(row=>`<tr><td>${esc(row.name)}<br/>${esc(row.code)} · ${esc(row.score??'—')}</td><td>${num(row.entry_price)}／${num(row.exit_price)}</td><td>${num(row.return_pct)}${row.return_pct==null?'':'%'}</td><td>${num(row.drawdown_pct)}${row.drawdown_pct==null?'':'%'}</td><td style="white-space:normal;min-width:280px">${esc(row.message)}<details><summary>原入选依据及已保存AI解读（不调用模型）</summary><ul>${(row.reasons||[]).map(text=>`<li>${esc(text)}</li>`).join('')}</ul>${row.ai?.status==='completed'?['reasons','risks','observe'].map(key=>`<ul>${(row.ai.content?.[key]||[]).map(item=>`<li>${esc(item.text)}</li>`).join('')}</ul>`).join(''):'当时未保存成功AI解读'}</details></td></tr>`).join('')}</tbody></table></div>`;
  }
  async function evaluate() {
    const id=$('candidateHistoryRun').value;
    if(!id){$('candidateHistoryStatus').textContent='请先查询并选择一条已保存名单';return;}
    const token=++serial; busy(true);
    $('candidateHistoryStatus').textContent='正在读取本地个股日线验证原名单，不重新选股…';
    try {
      const data=await api(`/api/candidates/runs/${encodeURIComponent(id)}/performance?days=${encodeURIComponent($('candidateHistoryDays').value)}`);
      if(token!==serial)return;
      render(data);$('candidateHistoryStatus').textContent='验证完成；未生成新候选，未调用AI。缺失行情请在通达信下载后重新验证。';
    } catch(error){if(token===serial)$('candidateHistoryStatus').textContent=`验证失败：${error.message}；原名单和结果保留`;}
    finally{if(token===serial)busy(false);}
  }
  $('candidateHistoryLoad').onclick=loadHistory;
  $('candidateHistoryEvaluate').onclick=evaluate;
  for(const id of ['candidateProvider','candidateType','candidateHistoryDate']) $(id).addEventListener('change',()=>{
    ++serial;busy(false);$('candidateHistoryRun').innerHTML='<option value="">条件已变化，请重新查询保存名单</option>';
    $('candidateHistoryResults').innerHTML='';$('candidateHistoryStatus').textContent='使用当前所选来源、范围和保存日期，请查询保存名单。';
  });
})();

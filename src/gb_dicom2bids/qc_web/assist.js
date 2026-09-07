"use strict";

async function renderAssistSubject(){
  const panel=$('#protocol-panel'), content=$('#protocol-content');
  panel.hidden=!current?.protocol_group;
  content.replaceChildren();
  if(panel.hidden)return;
  const subject=current.subject;
  try{
    const data=await api('/api/assist?group='+encodeURIComponent(current.protocol_group));
    if(current.subject!==subject)return;
    const group=data.group;
    content.append(element('p',`${group.center} · 同组合 ${group.count} 名 · ${group.published?'已发布规则':'尚未发布规则'}`));
    const representative=element('button','查看代表病例 '+group.representative);
    representative.onclick=()=>openSubject(group.representative);
    content.append(representative,element('p','名称与几何相同的模板复用模态和优先级。数字越小越优先；相同优先级保留多候选比较。质量决定仍逐幅处理。'));
    for(const conflict of group.conflicts){
      const line=element('div','冲突: '+conflict.reason,'error');
      if(conflict.subject){const link=element('button',conflict.subject);link.onclick=()=>openSubject(conflict.subject);line.append(link);}
      content.append(line);
    }
    const entries=[];
    for(const entry of group.templates){
      const row=element('div',undefined,'protocol-row');
      row.append(element('span',entry.example_name));
      const modality=element('select');
      for(const [value,label] of [['t1','T1'],['flair','FLAIR'],['other','其他']]){
        const option=element('option',label);option.value=value;modality.append(option);
      }
      modality.value=entry.modality;
      const priority=element('input');priority.type='number';priority.min=0;priority.max=999;priority.value=entry.priority;
      priority.setAttribute('aria-label','协议优先级');modality.setAttribute('aria-label','模板模态');
      row.append(modality,priority);content.append(row);
      entries.push({id:entry.id,modality,priority});
    }
    const preview=element('button','预览批量影响'),publish=element('button','发布这组规则'),revoke=element('button','撤回这组规则');
    publish.disabled=true;revoke.disabled=!group.published;
    content.append(preview,publish,revoke);
    const report=element('pre');content.append(report);
    let payload=null;
    const makePayload=()=>({group:group.id,revision:data.revision,inventory_digest:data.inventory_digest,
      reviewer:$('#reviewer').value||'zhenzong',
      templates:Object.fromEntries(entries.map(e=>[e.id,{modality:e.modality.value,priority:Number(e.priority.value)}]))});
    content.addEventListener('input',()=>{payload=null;publish.disabled=true;});
    content.addEventListener('change',()=>{payload=null;publish.disabled=true;});
    preview.onclick=async()=>{
      try{payload=makePayload();const result=await api('/api/assist/preview',payload);
        report.textContent=JSON.stringify({affected_subjects:result.affected_subjects,conflicts:result.conflicts,examples:result.changes.slice(0,8)},null,2);
        payload.preview_digest=result.preview_digest;publish.disabled=result.conflicts.length>0;
      }catch(error){message(error.message,true);}
    };
    publish.onclick=async()=>{
      try{if(!payload)return;await api('/api/assist/publish',payload);await openSubject(subject);await refreshList();message('协议规则已发布。请运行 features、calibrate / propose 更新质量队列。');}
      catch(error){message(error.message,true);}
    };
    revoke.onclick=async()=>{
      try{await api('/api/assist/revoke',{group:group.id,revision:data.revision});await openSubject(subject);message('规则已撤回，相关自动授权已失效；可用 --apply --dry-run 查看归档变化。');}
      catch(error){message(error.message,true);}
    };
  }catch(error){message(error.message,true);}
}

function addAssistQuality(quality,candidate){
  const select=element('select');select.className='failure-category';select.setAttribute('aria-label','质量原因');
  for(const [value,label] of [['','原因类别（可选）'],['motion_blur','头动 / 模糊'],['ghosting','重影'],['signal_loss','信号缺失'],['distortion','畸变'],['noise','噪声'],['coverage','覆盖不足'],['other_quality','其他质量问题'],['classification','模态分类错误'],['not_selected','仅未选中'],['unknown','原因待确认']]){
    const option=element('option',label);option.value=value;select.append(option);
  }
  select.value=rating(candidate).failure_category||'';
  select.onchange=()=>{
    const value=rating(candidate);value.failure_category=select.value;
    if(!value.reason&&select.value){value.reason=select.selectedOptions[0].textContent;quality.querySelector('textarea').value=value.reason;}
    changed();
  };
  quality.append(select);
  const status=candidate.assist;
  if(status){
    const state=({auto_pass:'自动通过',audit:'需要抽查',quality_review:'质量待复核',manual_resolved:'人工已决定'})[status.queue]||'等待质量计算';
    const metric=status.quality_score!=null?`质量评分 ${status.quality_score.toFixed(3)}（非准确率）`:
      status.risk_rank!=null?`同模板风险排序 ${status.risk_rank.toFixed(3)}（非概率）`:'';
    quality.append(element('p',`${state} · ${status.reason||''} · ${metric}`,'hint'));
  }
}

let assistProgressBusy=false;
async function refreshAssistProgress(){
  // Do not repeatedly read large audit tables while the initial workflow is pending.
  if(workflow===null||assistProgressBusy)return;
  if(identifying()){$('#assist-progress').textContent='当前仅识别序列；质量检查尚未开始。';return;}
  try{
    assistProgressBusy=true;
    const status=await api('/api/assist/status'), f=status.features, q=status.queues;
    if(identifying()){$('#assist-progress').textContent='当前仅识别序列；质量检查尚未开始。';return;}
    if(workflow?.enabled&&!status.queues_at){$('#assist-progress').textContent='尚未运行自动质量筛查；可进行人工质量检查。';return;}
    $('#assist-progress').textContent=`特征 ${f.completed||0}/${f.total||0} · 失败 ${f.failed||0} · 待复核 ${q.quality_review||0} · 抽查 ${q.audit||0} · 自动通过 ${q.auto_pass||0}`;
  }catch(error){$('#assist-progress').textContent=error.message;}
  finally{assistProgressBusy=false;}
}

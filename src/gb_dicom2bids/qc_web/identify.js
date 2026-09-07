"use strict";
let workflow=null, activeIdentificationGroup=null, identificationDraft=null;
let identificationShowAll=false, identificationDeferred=new Set(), identificationFailures=new Set();
const identifying=()=>workflow?.enabled&&workflow.phase==='identification';

function identificationCandidates(){
  const group=current.identification_groups?.find(g=>g.id===activeIdentificationGroup);
  if(!group||identificationShowAll)return current.candidates;
  const allowed=new Set(group.new_candidate_ids||current.candidates.map(c=>c.id));
  return current.candidates.filter(c=>allowed.has(c.id));
}

function identificationFailure(id){
  if(!identifying())return;
  // A technical error is not a human decision. Never check or lock a defer box here.
  identificationFailures.add(id);
  document.querySelector('#identify-publish')?.setAttribute('disabled','');
}

function displayWorkflow(value){
  workflow=value;
  document.body.classList.toggle('identifying',identifying());
  $('#workflow').hidden=!value.enabled;
  if(!value.enabled)return;
  const t=value.counts.t1,f=value.counts.flair;
  $('#workflow-status').textContent=identifying()
    ?`阶段 1 · 序列识别：T1 待识别 ${t.pending_groups} 组 / ${t.pending_subjects} 人；FLAIR 待识别 ${f.pending_groups} 组 / ${f.pending_subjects} 人。此阶段不判断图像质量。`
    :'阶段 2 · 质量检查：序列识别已完成。质量逐幅判断，不从代表病例复制。';
  $('#next-stage').textContent=identifying()?'序列识别完成，进入质量检查':'返回序列识别（暂停质量授权）';
  $('#next-stage').disabled=identifying()&&value.pending_groups>0;
  for(const option of $('#assist-queue').options){
    const sequence=['protocol','identified'].includes(option.value);
    option.disabled=identifying()?!sequence:sequence;
  }
  if($('#assist-queue').selectedOptions[0].disabled)$('#assist-queue').value=identifying()?'protocol':'';
}

async function loadWorkflow(signal){
  const value=await api('/api/identify',undefined,signal);
  if(!signal?.aborted)displayWorkflow(value);
}

async function renderIdentification(){
  const panel=$('#protocol-panel'),content=$('#protocol-content');
  panel.hidden=!identifying();
  content.replaceChildren();
  if(panel.hidden)return;
  panel.open=true;
  const options=current.identification_groups||[];
  let group=options.find(g=>g.id===activeIdentificationGroup)||options.find(g=>g.needs_protocol)||options[0];
  if(!group){content.append(element('p','此患者无待识别组。'));return;}
  activeIdentificationGroup=group.id;
  const data=await api('/api/identify?group='+encodeURIComponent(group.id));
  if(activeIdentificationGroup!==group.id)return;
  displayWorkflow(data);group=data.group;
  const slot=current.identification_groups.findIndex(g=>g.id===group.id);
  current.identification_groups[slot]=group;
  identificationDeferred=new Set(group.deferred_candidates||[]);
  for(const uid of group.failed_preview_candidates||[])identificationFailures.add(uid);
  const target=group.modality.toUpperCase();
  content.append(element('h3',`${target} 序列识别 · 同类 ${group.count} 人 · 待识别 ${group.pending_count} 人`));
  content.append(element('p','只确认序列归属与协议优先级。其他序列不参与分组；纠错时可从全部序列中选择。不同层数和体素保留在后续质量检查中。'));
  content.append(element('p',`有效 ${target} 候选归属明确、无目标候选冲突或待定时，该患者结束 ${target} 识别；无需继续排除无关序列。`,'hint'));
  content.append(element('p',`保留 ${target}、仅移出部分模板时，请在对应模板的下拉框选择“不是 ${target}（移出候选）”，再预览同类影响；预览失败不阻拦排除，手动勾选待定的模板仍受保护。`,'hint'));
  const excludedTargets=current.candidates.filter(c=>c.candidate_type===group.modality&&c.excluded_modalities?.includes(group.modality));
  if(excludedTargets.length)content.append(element('p',`以下带 ${target} 标签的序列已被本组排除，不是有效候选：${excludedTargets.map(c=>c.series_description).join('；')}。如需恢复，请展开“已排除模板 / 撤回排除”，撤回对应模板后再加入 ${target} 识别。`,'list-error'));
  content.append(element('p',`本轮新序列 ${group.new_template_count??0} 种 · 累计排除 ${group.excluded_template_count??0} 种 · 自动跳过 ${group.auto_skipped_subjects??0} 人 · 剩余待识别 ${group.pending_count} 人`));
  const showAllLabel=element('label',' 查看全部序列（含已排除）'),showAll=element('input');
  showAll.id='identify-show-all';showAll.type='checkbox';showAll.checked=identificationShowAll;showAllLabel.prepend(showAll);content.append(showAllLabel);
  const entries=new Map(group.templates.map(e=>[e.id,{...e}]));
  const rows=element('div');content.append(rows);
  const drawRows=()=>{
    rows.replaceChildren();
    for(const entry of entries.values()){
      const row=element('div',undefined,'protocol-row');
      row.append(element('span',`${entry.example_name} · 几何版本 ${entry.geometry_variants??1}`));
      const mode=element('select');mode.setAttribute('aria-label','序列归属');
      for(const [value,label] of [[group.modality,`属于 ${target}`],['other',`不是 ${target}（移出候选）`]]){
        const option=element('option',label);option.value=value;mode.append(option);
      }
      mode.value=entry.modality;
      mode.onchange=()=>{entry.modality=mode.value;invalidate();};
      const rank=element('input');rank.type='number';rank.min=0;rank.max=999;rank.value=entry.priority;
      rank.setAttribute('aria-label','协议优先级');rank.oninput=()=>{entry.priority=Number(rank.value);invalidate();};
      row.append(mode,rank);rows.append(row);
    }
  };
  const optional=element('div',undefined,'protocol-row'),select=element('select');
  select.setAttribute('aria-label','纠错备选序列');
  function fillOptional(){select.replaceChildren();for(const c of identificationCandidates()){const option=element('option',c.series_description);option.value=c.id;select.append(option);}}
  fillOptional();
  showAll.onchange=()=>{identificationShowAll=showAll.checked;$('#others').checked=showAll.checked;fillOptional();renderPanes();};
  const add=element('button',`将备选加入 ${target} 识别`);
  add.onclick=()=>{
    const c=current.candidates.find(c=>c.id===select.value);
    if(!c)return;
    entries.set(c.family_id,{id:c.family_id,example_name:c.series_description,modality:group.modality,priority:0});
    $('#others').checked=true;drawRows();invalidate();
    const pane=$('#panes .pane');if(pane)showCandidate(pane,c.id);
  };
  optional.append(select,add);content.append(optional);
  const compareLabel=element('label',' 同优先级不同协议留待质量阶段逐幅比较');
  const compare=element('input');compare.type='checkbox';compareLabel.prepend(compare);content.append(compareLabel);
  const reviewer=element('input');reviewer.value='zhenzong';reviewer.setAttribute('aria-label','序列审核者');content.append(reviewer);
  const reviewList=element('details');reviewList.open=true;
  reviewList.append(element('summary','本轮待确认的序列（勾选表示待定，不参与排除）'));
  reviewList.append(element('p','仅手动勾选项保留待定。点击本轮均不是目标并确认发布后，未勾选项将按同组模板排除，包括预览失败项；不改变质量记录。已保存的待定选择会恢复。','hint'));
  for(const c of current.candidates.filter(c=>(group.new_candidate_ids||[]).includes(c.id))){
    const label=element('label',' '+c.series_description),box=element('input');
    box.type='checkbox';box.dataset.deferUid=c.id;box.checked=identificationDeferred.has(c.id);
    box.setAttribute('aria-label','待定 '+c.series_description);
    box.onchange=()=>{if(box.checked)identificationDeferred.add(c.id);else identificationDeferred.delete(c.id);invalidate();};
    label.prepend(box);const row=element('div');row.append(label);reviewList.append(row);
  }
  content.append(reviewList);
  const preview=element('button','预览同类影响'),publish=element('button','确认识别并应用同类'),absent=element('button',`本轮序列均不是 ${target}`);
  publish.id='identify-publish';publish.disabled=true;
  content.append(preview,publish,absent);
  const undo=element('details');undo.append(element('summary','已排除模板 / 撤回排除'));
  const revoked=new Set();
  for(const entry of group.negative_templates||[]){
    const label=element('label',' '+entry.name),box=element('input');box.type='checkbox';
    box.setAttribute('aria-label','撤回 '+entry.name);
    box.onchange=()=>{if(box.checked)revoked.add(entry.id);else revoked.delete(entry.id);invalidate();};
    label.prepend(box);const row=element('div');row.append(label);undo.append(row);
  }
  const revoke=element('button','预览撤回排除');revoke.disabled=!(group.negative_templates||[]).length;
  undo.append(revoke);content.append(undo);
  const report=element('pre');content.append(report);
  let payload=null;
  function invalidate(){payload=null;publish.disabled=true;dirty=true;identificationDraft=true;message('序列规则尚未发布；此操作不保存质量通过。');}
  reviewer.oninput=compare.onchange=invalidate;
  const makePayload=()=>({group:group.id,subject:current.subject,revision:data.revision,
    reviewer:reviewer.value,compare_in_quality:compare.checked,
    templates:Object.fromEntries([...entries].map(([id,e])=>[id,{modality:e.modality,priority:e.priority}]))});
  async function doPreview(action='positive'){
    try{payload=makePayload();
      if(action==='positive'){
        // A mixed decision excludes only the explicitly rejected template rows,
        // never every unchecked optional sequence in the current round.
        const rejected=Object.entries(payload.templates).filter(([,entry])=>entry.modality==='other').map(([id])=>id);
        if(rejected.length){
          payload.negative_source_policy='identity_only';
          payload.negative_templates=rejected;
          payload.deferred_candidates=current.candidates.filter(c=>identificationDeferred.has(c.id)).map(c=>c.id);
        }
      }
      if(action==='negative'){
        const pending=current.candidates.filter(c=>(group.new_candidate_ids||[]).includes(c.id));
        const blocked=new Set(pending.filter(c=>identificationDeferred.has(c.id)).map(c=>c.family_id));
        payload.templates={};
        payload.negative_source_policy='identity_only';
        payload.negative_templates=[...new Set(pending.filter(c=>!blocked.has(c.family_id)).map(c=>c.family_id))];
        payload.deferred_candidates=current.candidates.filter(c=>identificationDeferred.has(c.id)).map(c=>c.id);
        if(!payload.negative_templates.length&&!payload.deferred_candidates.length)throw Error('本轮没有尚待确认的序列。');
      }
      if(action==='revoke'){payload.templates={};payload.revoke_negative=[...revoked];if(!revoked.size)throw Error('请先勾选要撤回的模板。');}
      const result=await api('/api/identify/preview',payload);
      const names=(result.negative_templates||[]).map(f=>current.candidates.find(c=>c.family_id===f)?.series_description||f);
      report.textContent=(names.length?'将确认以下模板不是 '+target+'（含未勾选的预览失败项；仅序列排除，不判断质量）：\n'+names.join('\n')+'\n\n':'')+JSON.stringify(result,null,2);payload.preview_digest=result.preview_digest;
      publish.disabled=result.conflicts.length>0;
    }catch(error){payload=null;publish.disabled=true;message(error.message,true);}
  }
  preview.onclick=()=>doPreview();absent.onclick=()=>doPreview('negative');revoke.onclick=()=>doPreview('revoke');
  publish.onclick=async()=>{
    try{if(!payload)return;const result=await api('/api/identify/publish',payload);
      dirty=false;identificationDraft=null;displayWorkflow({...result,enabled:true});
      current=null;activeIdentificationGroup=null;clearImages($('#panes'));$('#panes').replaceChildren();
      $('#protocol-panel').hidden=true;$('#protocol-content').replaceChildren();
      offset=0;await loadWorkflow();await refreshList(result.next_group);
      if(result.next_group){const next=await api('/api/identify?group='+encodeURIComponent(result.next_group));
        if(current?.subject!==next.group.representative||activeIdentificationGroup!==next.group.id)await openSubject(next.group.representative,next.group.id);}
      message(`序列规则已应用至 ${result.affected_subjects} 人；未写入任何质量通过记录。`);
    }catch(error){message(error.message,true);}
  };
  drawRows();
  renderPanes();
}

async function transitionStage(){
  if(dirty){message('请先保存当前修改。',true);return;}
  try{
    const target=identifying()?'quality':'identification';
    if(target==='identification'&&!confirm('返回后暂停新增质量授权；人工 QC 保留。是否继续？'))return;
    await api('/api/identify/transition',{revision:workflow.revision,phase:target});
    current=null;activeIdentificationGroup=null;offset=0;
    await loadWorkflow();clearImages($('#panes'));$('#panes').replaceChildren();
    $('#protocol-panel').hidden=true;await refreshList();
  }catch(error){message(error.message,true);}
}

"use strict";
let workflow=null, activeIdentificationGroup=null, identificationDraft=null;
const identifying=()=>workflow?.enabled&&workflow.phase==='identification';

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

async function loadWorkflow(){
  displayWorkflow(await api('/api/identify'));
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
  const target=group.modality.toUpperCase();
  content.append(element('h3',`${target} 序列识别 · 同类 ${group.count} 人 · 待识别 ${group.pending_count} 人`));
  content.append(element('p','只确认序列归属与协议优先级。其他序列不参与分组；纠错时可从全部序列中选择。不同层数和体素保留在后续质量检查中。'));
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
  for(const c of current.candidates){const option=element('option',c.series_description);option.value=c.id;select.append(option);}
  const add=element('button',`将备选加入 ${target} 识别`);
  add.onclick=()=>{
    const c=current.candidates.find(c=>c.id===select.value);
    entries.set(c.family_id,{id:c.family_id,example_name:c.series_description,modality:group.modality,priority:0});
    $('#others').checked=true;drawRows();invalidate();
    const pane=$('#panes .pane');if(pane)showCandidate(pane,c.id);
  };
  optional.append(select,add);content.append(optional);
  const compareLabel=element('label',' 同优先级不同协议留待质量阶段逐幅比较');
  const compare=element('input');compare.type='checkbox';compareLabel.prepend(compare);content.append(compareLabel);
  const reason=element('input');reason.className='protocol-reason';reason.placeholder='序列识别依据（必填，不是质量意见）';content.append(reason);
  const reviewer=element('input');reviewer.value='zhenzong';reviewer.setAttribute('aria-label','序列审核者');content.append(reviewer);
  const preview=element('button','预览同类影响'),publish=element('button','确认识别并应用同类'),absent=element('button',`本例未找到 ${target}`);
  publish.disabled=true;absent.hidden=group.families.length>0;
  content.append(preview,publish,absent);
  const report=element('pre');content.append(report);
  let payload=null;
  function invalidate(){payload=null;publish.disabled=true;dirty=true;identificationDraft=true;message('序列规则尚未发布；此操作不保存质量通过。');}
  reason.oninput=reviewer.oninput=compare.onchange=invalidate;
  const makePayload=()=>({group:group.id,subject:current.subject,revision:data.revision,
    reviewer:reviewer.value,reason:reason.value,compare_in_quality:compare.checked,
    templates:Object.fromEntries([...entries].map(([id,e])=>[id,{modality:e.modality,priority:e.priority}]))});
  async function doPreview(none=false){
    try{payload=makePayload();if(none)payload.absent=true;
      const result=await api('/api/identify/preview',payload);
      report.textContent=JSON.stringify(result,null,2);payload.preview_digest=result.preview_digest;
      publish.disabled=result.conflicts.length>0;
    }catch(error){payload=null;publish.disabled=true;message(error.message,true);}
  }
  preview.onclick=()=>doPreview();absent.onclick=()=>doPreview(true);
  publish.onclick=async()=>{
    try{if(!payload)return;const result=await api('/api/identify/publish',payload);
      dirty=false;identificationDraft=null;displayWorkflow({...result,enabled:true});
      current=null;activeIdentificationGroup=null;clearImages($('#panes'));$('#panes').replaceChildren();
      $('#protocol-panel').hidden=true;$('#protocol-content').replaceChildren();
      offset=0;await loadWorkflow();await refreshList();
      message(`序列规则已应用至 ${result.affected_subjects} 人；未写入任何质量通过记录。`);
    }catch(error){message(error.message,true);}
  };
  drawRows();
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

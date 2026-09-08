"use strict";
let ruleOffset=0, ruleData=null, rulePayload=null, ruleGeneration=0;
const ruleSelected=new Set();
const ruleKinds={include:'纳入 / 优先级',exclude:'排除',absent:'未找到目标',recheck:'重新人工识别'};
function invalidateRulePreview(){rulePayload=null;$('#rule-publish').disabled=true;$('#rule-report').textContent='';}
async function loadRules(){
  const serial=++ruleGeneration;ruleSelected.clear();invalidateRulePreview();
  $('#rule-status').textContent='读取已保存的规则索引… 不读取源影像';
  const params=new URLSearchParams({view:$('#rule-view').value,kind:$('#rule-kind').value,modality:$('#rule-modality').value,center:$('#rule-center').value.trim(),subject:$('#rule-subject').value.trim(),q:$('#rule-query').value.trim(),offset:ruleOffset});
  try{
    const data=await api('/api/identify/rules?'+params);if(serial!==ruleGeneration)return;ruleData=data;
    $('#rule-rows').replaceChildren();$('#rule-return-label').hidden=data.phase!=='quality';
    $('#rule-status').textContent=`共 ${data.total} 条 · 第 ${Math.floor(ruleOffset/100)+1} 页 · 规则版本 ${data.revision}`;
    $('#rule-prev').disabled=ruleOffset===0;$('#rule-next').disabled=ruleOffset+100>=data.total;
    for(const row of data.rules){
      const item=element('div',undefined,'rule-row'),label=element('label'),box=element('input');
      box.type='checkbox';box.disabled=!row.active;box.setAttribute('aria-label','撤销 '+row.name);
      box.onchange=()=>{if(box.checked)ruleSelected.add(row.id);else ruleSelected.delete(row.id);invalidateRulePreview();};
      label.append(box,document.createTextNode(`${row.name} · ${ruleKinds[row.kind]} · ${row.modality.toUpperCase()}`));
      item.append(label,element('small',`${row.center} · 影响 ${row.count} 人 · ${row.active?'当前生效':'历史记录（不操作）'} · ${row.reviewer||'未记录'} · ${row.saved_at||'时间未记录'}`));
      const details=element('details');details.append(element('summary','规则内容与范围'),element('pre',JSON.stringify({rule:row.value,scope:row.scope||'中心模板 / 指定患者',revision:row.revision},null,2)));item.append(details);
      if(row.example_subject){const view=element('button','查看代表患者');view.onclick=async()=>{$('#rule-review').close();await openSubject(row.example_subject);};item.append(view);}
      $('#rule-rows').append(item);
    }
  }catch(error){$('#rule-status').textContent=error.message;message(error.message,true);}
}
async function previewRuleRemoval(){
  invalidateRulePreview();
  try{
    if(!ruleSelected.size)throw Error('请勾选要撤销的当前规则。');
    if(!ruleData)throw Error('请先读取规则列表。');
    rulePayload={revision:ruleData.revision,rule_ids:[...ruleSelected],recheck:$('#rule-recheck').checked,reviewer:$('#rule-reviewer').value.trim(),return_to_identification:$('#rule-return').checked};
    const result=await api('/api/identify/revoke_preview',rulePayload);
    rulePayload.preview_digest=result.preview_digest;
    $('#rule-report').textContent=`撤销将影响 ${result.affected_subjects} 人，重新进入识别 ${result.reopened_subject_modalities} 个患者/模态。\n${result.manual_image_overrides?'其中 '+result.manual_image_overrides+' 人仍受人工影像决定约束；本操作不改那些决定。\n':''}${result.returns_to_identification?'将返回序列识别阶段，并暂停质量授权。\n':''}`+JSON.stringify(result,null,2);
    $('#rule-publish').disabled=result.returns_to_identification&&!rulePayload.return_to_identification;
    $('#rule-status').textContent='仅预览，尚未撤销。请核对影响范围后确认。';
  }catch(error){invalidateRulePreview();$('#rule-status').textContent=error.message;}
}
async function publishRuleRemoval(){
  try{
    if(!rulePayload)throw Error('请先预览撤销影响。');
    if(dirty)throw Error('当前影像质量决定尚未保存；请先关闭回顾并处理未保存编辑，再撤销规则。');
    const result=await api('/api/identify/revoke_publish',rulePayload);
    invalidateRulePreview();generation++;current=null;draft=null;$('#decision-bar').hidden=true;$('#protocol-panel').hidden=true;clearImages($('#panes'));$('#panes').replaceChildren();
    await loadWorkflow();await refreshList();await loadRules();
    $('#rule-status').textContent=`已撤销，影响 ${result.affected_subjects} 人。源影像、逐影像质量记录和 BIDS 未修改。`;
  }catch(error){$('#rule-status').textContent=error.message;$('#rule-publish').disabled=true;}
}
document.addEventListener('DOMContentLoaded',()=>{
  $('#open-rule-review').onclick=()=>{$('#rule-return').checked=false;$('#rule-review').showModal();ruleOffset=0;loadRules();};
  $('#close-rule-review').onclick=()=>$('#rule-review').close();
  $('#rule-search').onclick=()=>{ruleOffset=0;loadRules();};
  for(const id of ['rule-view','rule-kind','rule-modality'])$('#'+id).onchange=()=>{ruleOffset=0;loadRules();};
  for(const id of ['rule-recheck','rule-return','rule-reviewer'])$('#'+id).onchange=invalidateRulePreview;
  $('#rule-prev').onclick=()=>{ruleOffset=Math.max(0,ruleOffset-100);loadRules();};
  $('#rule-next').onclick=()=>{ruleOffset+=100;loadRules();};
  $('#rule-preview').onclick=previewRuleRemoval;$('#rule-publish').onclick=publishRuleRemoval;
});

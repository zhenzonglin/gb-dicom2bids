"use strict";
let identificationJobState={pending:[],recent:[]}, identificationJobTimer=null;
let submittingIdentification=false, receiptDraft=null, jobStateSignature='';
const identificationPrefetch=new Map();
const pendingIdentificationGroups=()=>new Set(identificationJobState.pending.map(j=>j.group));

function prefetchIdentification(id,group){
  if(!identificationPrefetch.has(group)){
    const promise=Promise.all([api('/api/subject?id='+encodeURIComponent(id)),api('/api/identify?group='+encodeURIComponent(group))])
      .then(([subject,snapshot])=>({subject,snapshot})).catch(()=>null);
    identificationPrefetch.set(group,promise);
    while(identificationPrefetch.size>6)identificationPrefetch.delete(identificationPrefetch.keys().next().value);
  }
  return identificationPrefetch.get(group);
}
function prefetchNextIdentification(){
  if(!identifying()||$('#assist-queue').value!=='protocol')return;
  const pending=pendingIdentificationGroups();
  for(const row of listing.filter(s=>s.identification_group!==activeIdentificationGroup&&!pending.has(s.identification_group)).slice(0,2))prefetchIdentification(row.id,row.identification_group);
}
async function takeIdentificationPrefetch(group){
  const pending=identificationPrefetch.get(group);
  identificationPrefetch.delete(group);
  return pending?await pending:null;
}
function renderIdentificationJobs(){
  const panel=$('#identification-jobs');panel.hidden=!workflow?.enabled;
  const jobs=identificationJobState;
  const failed=jobs.recent.filter(j=>j.state==='failed');
  $('#identification-job-status').textContent=`后台保存：排队/写入 ${jobs.pending.length} · 最近已保存 ${jobs.recent.filter(j=>j.state==='completed').length} · 最近失败 ${failed.length}（最近 50 条结果）。排队不等于已保存。`;
  $('#identification-job-status').className=failed.length||jobs.error?'error':'';
  const details=$('#identification-job-details');details.replaceChildren();
  if(jobs.error)details.append(element('p','后台存储错误，待办记录保留：'+jobs.error,'error'));
  for(const job of [...jobs.pending,...failed]){
    const row=element('div',`${job.subject} · ${job.modality?.toUpperCase()||''} · ${job.state==='failed'?'保存失败':job.state==='running'?'正在保存':'排队中'}${job.error?'：'+job.error:''}`);
    if(job.state==='failed'){
      const button=element('button','返回该组复核');button.onclick=async()=>{
        try{
          if(dirty&&!confirm('当前修改未提交，确定放弃并返回失败组？'))return;
          const data=await api('/api/subject?id='+encodeURIComponent(job.subject));
          const group=data.identification_groups.find(g=>g.id===job.group)||data.identification_groups.find(g=>g.modality===job.modality);
          if(!group)throw Error('原组已变化，请在全部协议组中按患者查找。');
          dirty=false;$('#assist-queue').value='identified';
          await openSubject(job.subject,group.id,{subject:data,snapshot:await api('/api/identify?group='+group.id)});
          message(job.error,true);
        }catch(error){message(error.message,true);}
      };row.append(button);
    }
    details.append(row);
  }
  $('#retry-identification-receipt').hidden=!receiptDraft;
  if(jobs.pending.length||submittingIdentification||receiptDraft)$('#next-stage').disabled=true;
}
async function loadIdentificationJobs(){
  identificationJobState=await api('/api/identify/jobs');
  if(!jobStateSignature)jobStateSignature=JSON.stringify(identificationJobState.recent.map(j=>[j.id,j.state]));
  const key='gb-identification-receipt-'+identificationJobState.scope;
  if(!receiptDraft){try{receiptDraft=JSON.parse(sessionStorage.getItem(key)||'null');}catch{receiptDraft=null;}}
  if(receiptDraft){
    const id=receiptDraft.request_id.replaceAll('-','');
    if([...identificationJobState.pending,...identificationJobState.recent].some(j=>j.id===id)){
      receiptDraft=null;sessionStorage.removeItem(key);
    }
  }
  renderIdentificationJobs();
  clearTimeout(identificationJobTimer);
  identificationJobTimer=setTimeout(pollIdentificationJobs,1500);
}
document.querySelector('#retry-identification-receipt').onclick=sendIdentificationReceipt;
async function pollIdentificationJobs(){
  try{
    await loadIdentificationJobs();
    const signature=JSON.stringify(identificationJobState.recent.map(j=>[j.id,j.state]));
    if(jobStateSignature&&signature!==jobStateSignature){
      // Never replace the open draft or its basis with another revision's interpretation.
      if(!current&&!dirty&&!submittingIdentification)await refreshList();
      else if(!identificationJobState.pending.length)await loadWorkflow();
    }
    jobStateSignature=signature;
  }catch(error){
    $('#identification-job-status').textContent='无法查询后台保存状态：'+error.message;
    $('#identification-job-status').className='error';
    identificationJobTimer=setTimeout(pollIdentificationJobs,3000);
  }
}
async function queueIdentification(payload){
  if(submittingIdentification)return;
  if(receiptDraft)throw Error('上次提交尚未收到回执，请先重试原请求，避免重复提交。');
  receiptDraft={request_id:crypto.randomUUID(),payload:structuredClone(payload)};
  sessionStorage.setItem('gb-identification-receipt-'+identificationJobState.scope,JSON.stringify(receiptDraft));
  await sendIdentificationReceipt();
}
async function sendIdentificationReceipt(){
  if(!receiptDraft||submittingIdentification)return;
  submittingIdentification=true;dirty=true;renderIdentificationJobs();
  const raw=receiptDraft;
  try{
    const job=await api('/api/identify/submit',raw);
    receiptDraft=null;sessionStorage.removeItem('gb-identification-receipt-'+identificationJobState.scope);
    if(job.state==='failed')throw Error(job.error||'原请求保存失败，请返回该组复核');
    if(!identificationJobState.pending.some(j=>j.id===job.id)&&job.state!=='completed')identificationJobState.pending.push(job);
    dirty=false;identificationDraft=null;
    const group=raw.payload.group;
    listing=listing.filter(s=>s.identification_group!==group);
    for(const button of $('#subjects').children)if(button.dataset.group===group)button.remove();
    current=null;activeIdentificationGroup=null;generation++;
    clearImages($('#panes'));$('#panes').replaceChildren();$('#protocol-panel').hidden=true;
    $('#decision-bar').hidden=true;$('#subject-title').textContent='选择一位受试者';
    const next=listing.find(s=>!pendingIdentificationGroups().has(s.identification_group));
    if(next)await openSubject(next.id,next.identification_group);
    else $('#panes').append(element('section','本页已提交。后台保存完成后自动加载剩余待识别组。','empty'));
    message('操作已进入私有后台队列，正在保存；可继续下一组。请查看后台保存结果。');
  }catch(error){message('提交未完成：'+error.message,true);}
  finally{submittingIdentification=false;renderIdentificationJobs();}
}

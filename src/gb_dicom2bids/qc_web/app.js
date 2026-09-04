"use strict";
const $ = s => document.querySelector(s);
const token = $('meta[name="qc-token"]').content;
let listing = [], current = null, draft = null, dirty = false, offset = 0, generation = 0;
const autoNames = {selected:"自动入选",review:"待复核",excluded:"排除"};
function message(text, error=false){$('#message').textContent=text;$('#message').className=error?'error':'';}
async function api(path, data){const response=await fetch(path,{method:data?'POST':'GET',headers:{'X-QC-Token':token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});const result=await response.json();if(!response.ok)throw Error(result.error||response.statusText);return result;}
function element(tag, text, className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function changed(){dirty=true;message('有未保存决定；保存后才进入私有QC记录。');updateChoices();}
async function refreshList(){try{const params=new URLSearchParams({q:$('#search').value,center:$('#center').value,protocol:$('#protocol').value,status:$('#auto-status').value,reason:$('#reason-filter').value,pilot:$('#pilot').checked?'1':'0',pending:$('#pending').checked?'1':'0',others:$('#others').checked?'1':'0',offset});const data=await api('/api/subjects?'+params);listing=data.subjects;$('#total').textContent=data.total;$('#subjects').replaceChildren();for(const subject of listing){const button=element('button',subject.id,'subject'+(current?.subject===subject.id?' active':''));button.append(element('small',`${subject.center} · T1 ${subject.t1} / FLAIR ${subject.flair} / 其他 ${subject.other}`),element('small',`已评 ${subject.reviewed} · 已定模态 ${subject.resolved}${subject.pilot?' · pilot':''}`));button.onclick=()=>openSubject(subject.id);$('#subjects').append(button);}if(!current&&listing.length)await openSubject(listing[0].id);message('队列已载入；自动推荐不是人工通过。');}catch(error){message(error.message,true);}}
async function openSubject(id){if(dirty&&!confirm('当前决定尚未保存，确定放弃修改？'))return;try{current=await api('/api/subject?id='+encodeURIComponent(id));draft=structuredClone(current.decision);dirty=false;generation++;$('#subject-title').textContent='sub-'+id;$('#decision-bar').hidden=false;$('#reviewer').value=draft.reviewer||'zhenzong';for(const modality of ['t1','flair'])$('#none-reason-'+modality).value=draft.groups[modality]?.reason||'';renderPanes();updateChoices();$('#revision').textContent=`记录版本 ${draft.revision}`;document.querySelectorAll('.subject').forEach(b=>b.classList.toggle('active',b.firstChild.textContent===id));message('可逐层浏览。图像未准备时只准备当前候选，不写入BIDS。');}catch(error){message(error.message,true);}}
function candidates(){return current.candidates.filter(c=>$('#others').checked||(draft.candidates[c.id]?.modality||c.candidate_type)!=='other').sort((a,b)=>({selected:0,review:1,excluded:2}[a.auto_status]-{selected:0,review:1,excluded:2}[b.auto_status]));}
function rating(candidate){return draft.candidates[candidate.id]||(draft.candidates[candidate.id]={quality:'unreviewed',modality:candidate.candidate_type,reason:''});}
function clearImages(node){node.querySelectorAll('img[src^="blob:"]').forEach(img=>URL.revokeObjectURL(img.src));}
function comparisonCandidates(list){const chosen=[];const modality=c=>draft.candidates[c.id]?.modality||c.candidate_type;for(const target of ['t1','flair']){const candidate=list.find(c=>modality(c)===target&&!chosen.includes(c));if(candidate)chosen.push(candidate);}for(const candidate of list){if(chosen.length===2)break;if(!chosen.includes(candidate))chosen.push(candidate);}return chosen;}
function renderPanes(){clearImages($('#panes'));const list=candidates(),shown=comparisonCandidates(list);$('#panes').replaceChildren();for(let i=0;i<shown.length;i++){const pane=element('section',undefined,'pane');pane.dataset.pane=i;$('#panes').append(pane);showCandidate(pane,shown[i].id);}if(!list.length)$('#panes').append(element('section','本组没有可显示候选；请核对NIfTI清单。','empty'));}
async function showCandidate(pane,id){const candidate=current.candidates.find(c=>c.id===id), epoch=generation;pane.dataset.uid=id;clearImages(pane);pane.replaceChildren();const head=element('div',undefined,'pane-head'),select=element('select');for(const c of candidates()){const option=element('option',`${c.series_description} · ${c.candidate_type.toUpperCase()} · ${autoNames[c.auto_status]}`);option.value=c.id;select.append(option);}select.value=id;select.onchange=()=>showCandidate(pane,select.value);head.append(element('b',`展示序列 ${Number(pane.dataset.pane)+1}`),select,element('span',autoNames[candidate.auto_status],'badge '+candidate.auto_status),element('span',`${candidate.plane} · ${candidate.source_kind}`,'badge'));pane.append(head);const state=element('div','准备影像…','candidate-state');pane.append(state);const grid=element('div',undefined,'view-grid');pane.append(grid);const tools=element('div',undefined,'tools');pane.append(tools);const metadata=element('div',undefined,'metadata');metadata.textContent=`原始原因: ${candidate.auto_reason}\n序列: ${candidate.series_description} / ${candidate.protocol_name}\nStudy: ${candidate.study_uid_hash}  Series: ${candidate.series_uid_hash}\n${candidate.manufacturer} / ${candidate.model_name}  ${candidate.acquisition_type}\n体素平面 ${candidate.pixel_spacing_mm} mm · 层厚 ${candidate.slice_thickness_mm??'?'} mm · 覆盖 ${candidate.coverage_mm??'?'} mm\nTR/TE/TI: ${candidate.repetition_time_ms??'?'} / ${candidate.echo_time_ms??'?'} / ${candidate.inversion_time_ms??'?'} ms\n协议 ${candidate.protocol_id}\n检查/采集时间: ${Object.entries(candidate.timing).filter(([,v])=>v).map(([k,v])=>k+'='+v).join(' ')||'未提供'}`;pane.append(metadata);const quality=element('div',undefined,'quality'),buttons=element('div');for(const [value,label] of [['unreviewed','未评'],['pass','通过'],['fail','不通过'],['defer','待核实']]){const button=element('button',label);button.dataset.value=value;button.classList.toggle('chosen',rating(candidate).quality===value);button.onclick=()=>{rating(candidate).quality=value;buttons.querySelectorAll('button').forEach(b=>b.classList.toggle('chosen',b.dataset.value===value));changed();};buttons.append(button);}quality.append(buttons);const reason=element('textarea');reason.placeholder='失败、改分类或其他复核意见；人工改分类必须填写原因';reason.value=rating(candidate).reason||'';reason.oninput=()=>{rating(candidate).reason=reason.value;changed();};quality.append(reason);const row=element('div'),modality=element('select');for(const [value,label] of [['t1','指定为 T1'],['flair','指定为 FLAIR'],['other','指定为其他']]){const option=element('option',label);option.value=value;modality.append(option);}modality.value=rating(candidate).modality;modality.onchange=()=>{rating(candidate).modality=modality.value;changed();};const choose=element('button','设为最终候选');choose.onclick=()=>{const m=rating(candidate).modality;if(!['t1','flair'].includes(m)||rating(candidate).quality!=='pass'){message('先确认模态并将该候选标为通过。',true);return;}draft.groups[m]={choice:id,none:false,reason:''};changed();};row.append(modality,choose);quality.append(row);pane.append(quality);const details=element('details'),summary=element('summary','准备日志 / 错误'),log=element('pre','尚无日志');details.append(summary,log);pane.append(details);let response;try{response=await api('/api/prepare',{id});while(epoch===generation&&pane.dataset.uid===id&&pane.isConnected){state.textContent=({queued:'等待准备预览',converting:'正在准备当前候选',paused_resources:'资源不足，预览队列暂停',ready:'可阅片 · 滚轮切层，Ctrl+滚轮缩放，拖动平移',failed:'候选准备失败',interrupted:'预览已中断'}[response.state]||response.state)+(response.error?' · '+response.error:'');log.textContent=response.log||'暂无日志';if(response.state==='ready'){await buildViews(grid,tools,candidate,response.metadata,epoch);break;}if(response.state==='failed'||response.state==='interrupted'){const retry=element('button','重试预览');retry.onclick=async()=>{await api('/api/prepare',{id,retry:true});showCandidate(pane,id);};state.append(retry);break;}await new Promise(resolve=>setTimeout(resolve,1500));response=await api('/api/candidate?id='+id);}}catch(error){state.textContent=error.message;message(error.message,true);}}
async function buildViews(grid,tools,candidate,meta,epoch){
  if(epoch!==generation)return;
  tools.replaceChildren();
  grid.replaceChildren();
  let low=meta.window[0],high=meta.window[1];
  tools.append(element('span','窗低 / 高'));
  const a=element('input'),b=element('input');
  a.type=b.type='number';
  a.value=low.toFixed(1);
  b.value=high.toFixed(1);
  tools.append(a,b);
  const reset=element('button','复位视图');
  tools.append(reset);
  if(meta.errors.length)tools.append(element('span','不可通过: '+meta.errors.join('; ')));

  const view=element('div',undefined,'view source-view');
  const frame=element('div',undefined,'frame');
  const img=element('img');
  img.alt='原始体素切片';
  img.draggable=false;
  frame.append(img);
  const labels=['i=0',`i=${meta.shape[0]-1}`,`j=${meta.shape[1]-1}`,'j=0'];
  ['left','right','top','bottom'].forEach((pos,i)=>frame.append(element('span',labels[i],'orient '+pos)));
  const caption=element('div',undefined,'view-caption'),position=element('span');
  caption.append(element('span','原始体素切片（第三维）'),position);
  const slider=element('input');
  slider.type='range';
  slider.min=0;
  slider.max=meta.slice_count-1;
  slider.step=1;
  slider.value=meta.initial_slice;
  const initialSlice=meta.initial_slice;
  view.append(frame,caption,slider);
  grid.append(view);

  let zoom=1,dx=0,dy=0,last=null,serial=0,blobURL=null;
  const transform=()=>{img.style.transform=`translate(${dx}px,${dy}px) scale(${zoom})`;};
  async function draw(){
    const request=++serial;
    position.textContent=`第 ${Number(slider.value)+1} / ${meta.slice_count} 层`;
    try{
      const params=new URLSearchParams({id:candidate.id,index:slider.value,low,high});
      const response=await fetch('/api/slice?'+params,{headers:{'X-QC-Token':token}});
      if(!response.ok)throw Error((await response.json()).error);
      const blob=await response.blob();
      if(epoch!==generation||request!==serial||!view.isConnected)return;
      if(blobURL)URL.revokeObjectURL(blobURL);
      blobURL=URL.createObjectURL(blob);
      img.src=blobURL;
    }catch(error){message(error.message,true);}
  }
  slider.oninput=draw;
  frame.onwheel=event=>{
    event.preventDefault();
    if(event.ctrlKey){
      zoom=Math.max(.5,Math.min(8,zoom*(event.deltaY<0?1.15:.87)));
      transform();
    }else{
      const next=Number(slider.value)+(event.deltaY<0?1:-1);
      slider.value=Math.max(Number(slider.min),Math.min(Number(slider.max),next));
      draw();
    }
  };
  frame.onpointerdown=event=>{last=[event.clientX,event.clientY];frame.setPointerCapture(event.pointerId);};
  frame.onpointermove=event=>{if(!last)return;dx+=event.clientX-last[0];dy+=event.clientY-last[1];last=[event.clientX,event.clientY];transform();};
  frame.onpointerup=()=>{last=null;};
  a.onchange=b.onchange=()=>{
    low=+a.value;
    high=+b.value;
    if(high<=low){message('窗高必须大于窗低',true);return;}
    draw();
  };
  reset.onclick=()=>{zoom=1;dx=dy=0;slider.value=initialSlice;transform();draw();};
  await draw();
}
function updateChoices(){if(!draft)return;for(const modality of ['t1','flair']){const group=draft.groups[modality]||{};const candidate=current.candidates.find(c=>c.id===group.choice);$('#choice-'+modality).textContent=group.none?'无可用候选':candidate?`${candidate.series_number??''} ${candidate.series_description}`:'未确定';}}
async function save(next=false){if(!draft)return;try{draft.reviewer=$('#reviewer').value;for(const modality of ['t1','flair'])if(draft.groups[modality])draft.groups[modality].reason=$('#none-reason-'+modality).value;draft=await api('/api/save',{subject:current.subject,decision:draft});dirty=false;$('#revision').textContent=`记录版本 ${draft.revision}`;message('决定已保存。staging尚未改变；需在终端执行应用。');if(next){const index=listing.findIndex(s=>s.id===current.subject);if(index+1<listing.length)await openSubject(listing[index+1].id);else{offset+=100;current=null;await refreshList();}}}catch(error){message(error.message,true);}}
let timer;['search','center','protocol','reason-filter'].forEach(id=>$('#'+id).oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{offset=0;refreshList();},350);});['auto-status','pilot','pending'].forEach(id=>$('#'+id).onchange=()=>{offset=0;refreshList();});$('#others').onchange=()=>{offset=0;refreshList();if(current)renderPanes();};$('#prev-page').onclick=()=>{offset=Math.max(0,offset-100);refreshList();};$('#next-page').onclick=()=>{offset+=100;refreshList();};document.querySelectorAll('[data-none]').forEach(button=>button.onclick=()=>{const m=button.dataset.none;draft.groups[m]={choice:null,none:true,reason:$('#none-reason-'+m).value};changed();});document.querySelectorAll('[data-clear]').forEach(button=>button.onclick=()=>{delete draft.groups[button.dataset.clear];changed();});['reviewer','none-reason-t1','none-reason-flair'].forEach(id=>$('#'+id).oninput=changed);$('#save').onclick=()=>save();$('#save-next').onclick=()=>save(true);window.onbeforeunload=event=>{if(dirty){event.preventDefault();event.returnValue='';}};document.addEventListener('keydown',event=>{if(event.ctrlKey&&event.key==='s'){event.preventDefault();save();}});refreshList();

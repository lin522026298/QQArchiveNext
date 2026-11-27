'use strict';
const boot=window.QQ_BOOTSTRAP;
delete window.QQ_BOOTSTRAP;
const $=id=>document.getElementById(id);
const titles={overview:'概览',chats:'聊天记录',backups:'备份与恢复',health:'状态与处理',jobs:'任务',settings:'设置',agent:'Agent 接口'};
const labels={queued:'排队中',running:'运行中',completed:'已完成',cancelled:'已取消',failed:'失败',interrupted:'上次未完成',idle:'等待变化',capturing:'采集消息',media:'检查媒体',backup:'备份中',verify:'校验中',stopped:'已停止',retry:'等待重试',ok:'正常'};
let status={},accounts=[],conversations=[],current={type:'messages',conversation:null},page=[],searchOffset=0,lastSearch='',pending=new Set(),activeJobs=new Set(),refreshing=false,readGeneration=0,conversationGeneration=0;
let healthKey='',jobsKey='';
const el=(tag,text,cls)=>{const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(cls)item.className=cls;return item;};
const number=value=>Number(value||0).toLocaleString('zh-CN');
const date=value=>value?new Date(Number(value)>1e10?Number(value):Number(value)*1000).toLocaleString('zh-CN',{hour12:false}):'时间未知';
const bytes=value=>value===undefined||value===null?'—':(Number(value)/1024**3).toFixed(2)+' GB';
function notice(text,error=false){$('notice').replaceChildren(el('span',text));$('notice').classList.toggle('error',error);$('notice').hidden=!text;}
async function request(path,body){
  const response=await fetch(path,{method:body===undefined?'GET':'POST',cache:'no-store',headers:{'X-QQ-Token':boot.token,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body)});
  const value=await response.json();if(!response.ok)throw Error(value.error||'本地请求失败');return value;
}
function attempt(fn){return async()=>{try{await fn();}catch(error){notice(error.message,true);}};}
function account(){return $('account').value;}
async function call(operation,args={}){
  const task=await request('/api/call',{operation,arguments:args});pending.add(task.id);activeJobs.add(task.id);$('busy').hidden=false;
  notice((operation in (window.operationTitles||{})?window.operationTitles[operation]:operation)+'：正在处理…');
  const cancel=el('button','取消此任务');cancel.className='cancel-inline';cancel.addEventListener('click',attempt(async()=>{await request('/api/cancel',{id:task.id});cancel.disabled=true;cancel.textContent='正在取消…';}));$('notice').append(' ',cancel);
  try{
    while(true){
      await new Promise(resolve=>setTimeout(resolve,350));
      const value=await request('/api/job?id='+encodeURIComponent(task.id));
      if(value.state==='completed'){if(pending.size===1)notice('');return value.result;}
      if(['failed','cancelled','interrupted'].includes(value.state))throw Error(value.state==='cancelled'?'任务已取消':value.error||labels[value.state]);
    }
  }finally{pending.delete(task.id);activeJobs.delete(task.id);$('busy').hidden=pending.size===0;}
}
function show(name){
  const route=name;name=name.split('/')[0];
  if(!titles[name])name='overview';
  document.querySelectorAll('[data-view]').forEach(view=>view.hidden=view.dataset.view!==name);
  document.querySelectorAll('[data-page]').forEach(button=>button.classList.toggle('selected',button.dataset.page===name));
  $('page-title').textContent=titles[name];
  if(location.hash.slice(1)!==route||!route)history.replaceState(null,'','#'+name);
  if(name==='jobs')renderJobs(status.jobs||[]);
  if(name==='health')renderHealth();
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{status=await request('/api/status');renderStatus();if(!accounts.length)await loadAccounts();}
  finally{refreshing=false;}
}
function renderStatus(){
  const capture=status.capture||{},running=status.running;
  $('connection').textContent=running?'后台正在归档 · '+(labels[capture.state]||capture.state):'归档已暂停 · 可以继续浏览和恢复';
  $('archive-state').textContent=running?(labels[capture.state]||capture.state):'已暂停';
  const sources=$('sources');sources.replaceChildren();
  Object.entries(capture.sources||{}).forEach(([id,item])=>{
    const box=el('div',undefined,'source');box.append(el('strong',id+' · '+(labels[item.state]||item.state||'等待首次采集')));
    box.append(el('div','最近成功：'+date(item.last_success), 'muted'));
    if(item.media)box.append(el('div','媒体 '+number(item.media.files_seen)+' 个 · '+Number(item.media.seconds||0).toFixed(2)+' 秒 · 新增 '+number(item.media.observations_added)+' · 不可用 '+number(item.media.unavailable),'muted'));
    if(item.capture_issue)box.append(el('div',item.capture_issue.title+' · 下次检查 '+date(item.capture_issue.next_retry),'muted'));
    if(item.media_issue)box.append(el('div','媒体：'+item.media_issue.title+' · 下次检查 '+date(item.media_issue.next_retry),'muted'));
    if(item.error&&!item.capture_issue)box.append(el('div',item.error,'muted'));
    if(item.capture_issue||item.media_issue){const reason=el('button','查看原因与处理');reason.addEventListener('click',()=>show('health'));box.append(reason);}sources.append(box);
  });
  $('origins').textContent=JSON.stringify({state_directory:status.state_directory,application:status.application,priority:capture.priority,legacy_sources:status.legacy_sources,history_packages:status.history_packages,recovery_packages:status.recovery_packages},null,2);
  if(!$('repository').value&&status.repository)$('repository').value=status.repository;
  if(!document.querySelector('#settings-form :focus'))fillSettings(status.settings||{});
  if(!document.querySelector('#desktop-form :focus'))for(const key of ['launch_at_login','start_in_tray','close_to_tray','important_notifications'])$('desktop-form').elements[key].checked=!!status.desktop?.[key];
  $('desktop-status').textContent=status.desktop?.startup_needs_refresh?'程序路径已变化，请重新启用登录自启以更新入口。':'登录自启仅作用于当前用户；手动打开快捷方式始终显示窗口。';
  const web=status.web||{};$('web-link').hidden=!web.enabled;$('web-link').textContent=web.url||'';if(web.url)$('web-link').href=web.url;
  $('web-card').hidden=boot.scope!=='native';
  if(status.state_directory){const root=status.state_directory;const core=status.agent_executable;const command=core?'& "'+core+'"':'python -m qq_archive_next';$('agent-command').textContent=command+' --state "'+root+'" agent capabilities\n\n'+command+' --state "'+root+'" agent call status\n\n详情：GUI与Agent使用说明.md';}
  renderJobs(status.jobs||[]);
  renderHealth();
}

function renderHealth(){
  const health=status.health||{issues:[],events:[]},issues=health.issues||[];
  $('health-count').textContent=health.important_count?'('+health.important_count+')':'';
  $('health-summary').textContent=health.important_count?'有 '+health.important_count+' 项需要处理的重要事项。':health.retry_count?'后台正在自动重试，保留上次成功资料，无需处理普通重试。':status.running?'各来源运行正常。':'持续归档已暂停，已有资料仍可浏览、验证和恢复。';
  const key=JSON.stringify([issues,health.events,status.source_details]);
  if(key===healthKey){highlightIncident();return;}healthKey=key;
  const opened=new Set(Array.from($('health-issues').querySelectorAll('details[open]')).map(detail=>detail.parentElement.dataset.incident));
  const openedEvents=new Set(Array.from($('health-events').querySelectorAll('details[open]')).map(detail=>detail.dataset.sequence));
  $('health-issues').replaceChildren();
  for(const item of issues){
    const box=el('div',undefined,'health-issue '+(item.important?'important':''));box.dataset.incident=item.id;
    box.append(el('h3',item.title),el('p',item.summary),el('p',item.action));
    if(item.source_id)box.append(el('div','来源：'+item.source_id+(item.component?' · '+item.component:''),'muted'));
    if(item.next_retry)box.append(el('div','已重试 '+number(item.count)+' 次 · 下次 '+date(item.next_retry),'muted'));
    const detail=el('details');detail.open=opened.has(item.id);detail.append(el('summary','具体原因'),el('pre',item.detail||''));box.append(detail);
    const actions=el('div',undefined,'actions');
    if(item.source_id){const retry=el('button','重新检查此来源');retry.addEventListener('click',attempt(async()=>{await call('source.retry',{source_id:item.source_id});await refresh();notice('已安排重新检查；结果会在此处更新。');}));actions.append(retry);}
    if(item.code==='worker_stopped'||item.id.startsWith('worker:')){const restart=el('button','开启持续归档');restart.addEventListener('click',attempt(async()=>{await call('archive.start');await refresh();}));actions.append(restart);}
    if(item.job_id){const task=el('button','查看任务原因');task.addEventListener('click',attempt(async()=>{const value=await request('/api/job?id='+item.job_id);openInspector('任务结果').append(el('pre',JSON.stringify(value,null,2)));}));actions.append(task);}
    const backups=el('button','打开备份与恢复');backups.addEventListener('click',attempt(async()=>{show('backups');await listBackups();}));actions.append(backups);box.append(actions);$('health-issues').append(box);
  }
  $('health-sources').textContent=JSON.stringify(status.source_details||[],null,2);
  $('health-events').replaceChildren();
  for(const item of (health.events||[]).slice().reverse()){const box=el('details');box.dataset.sequence=String(item.sequence);box.open=openedEvents.has(String(item.sequence));box.append(el('summary',date(item.time)+' · '+(item.kind==='recovered'?'恢复正常':item.title||item.kind)+(item.source_id?' · '+item.source_id:'')),el('pre',JSON.stringify(item,null,2)));$('health-events').append(box);}
  if(!issues.length)$('health-issues').append(el('p','没有待处理故障。','muted'));
  highlightIncident();
}
function highlightIncident(){
  let id='';try{if(location.hash.startsWith('#health/'))id=decodeURIComponent(location.hash.slice(8));}catch(error){}
  for(const box of $('health-issues').children)box.classList.toggle('selected-incident',box.dataset.incident===id);
}
async function loadAccounts(){
  accounts=await call('accounts');const old=account();$('account').replaceChildren();
  for(const item of accounts){const option=el('option',item.account_id||item.account_key);option.value=item.account_key;$('account').append(option);}
  if(accounts.some(item=>item.account_key===old))$('account').value=old;
  renderMetrics();
}
function renderMetrics(){
  const selected=accounts.find(item=>item.account_key===account())||{};
  $('metrics').replaceChildren();
  for(const [name,value] of [['私聊记录',selected.c2c_messages],['群聊记录',selected.group_messages],['媒体对象',selected.media_objects]]){const box=el('div',undefined,'metric');box.append(el('span',name),el('strong',number(value)));$('metrics').append(box);}
}
async function loadConversations(){
  const generation=++conversationGeneration,selected=account();
  const values=await call('conversations',{account:selected,query:$('conversation-query').value,kind:$('conversation-kind').value});
  if(generation!==conversationGeneration||selected!==account())return;conversations=values;
  $('conversations').replaceChildren();
  for(const item of conversations){const button=el('button',undefined,'conversation');button.dataset.key=item.key;button.append(el('strong',item.label),el('small',item.kind+' · '+number(item.message_count)+' 条'));button.addEventListener('click',attempt(async()=>{current={type:'messages',conversation:item};await loadPage();}));$('conversations').append(button);}
  if(!conversations.length)$('conversations').append(el('p','暂无符合条件的会话','empty'));
}
async function loadPage(extra={}){
  if(current.type==='messages'&&!current.conversation){notice('请先选择会话，或打开全部会话时间线');return;}
  const args={account:account(),...extra};if(current.conversation)args.conversation=current.conversation.key;
  const generation=++readGeneration;const values=await call(current.type,args);
  if(generation!==readGeneration||args.account!==account())return;page=values;
  document.querySelectorAll('.conversation').forEach(button=>button.classList.toggle('active',button.dataset.key===current.conversation?.key));
  $('conversation-title').textContent=current.type==='timeline'?'全部会话时间线':current.conversation.label;
  $('search-paging').hidden=true;renderMessages(page);
}
function cursor(item){return {timestamp:item.timestamp,msg_id:item.msg_id};}
function renderMessages(values){
  $('messages').replaceChildren();$('page-count').textContent=values.length+' 条';
  if(!values.length)$('messages').append(el('p','此范围没有更多记录','empty'));
  for(const item of values){
    const box=el('article',undefined,'message'+(item.sent?' sent':''));box.dataset.messageId=item.msg_id;
    const head=el('div',undefined,'message-head');head.append(el('span',(item.sent?'我 · ':'')+(item.sender_label?item.sender_label+' · ':'')+(item.sender_id_kind||'')+' '+item.sender_id),el('time',date(item.timestamp)));box.append(head);
    box.append(el('div',item.text||'〔媒体或系统消息〕','message-body'));
    const actions=el('div',undefined,'message-actions');const raw=el('button','来源与版本');raw.addEventListener('click',attempt(()=>inspectRaw(item)));actions.append(raw);
    const media=el('button','媒体'+(item.media&&item.media.length?' · '+item.media.length:''));media.addEventListener('click',attempt(()=>inspectMedia(item,false)));actions.append(media);
    box.append(actions);$('messages').append(box);
  }
}
async function search(){
  const query=$('search-query').value.trim();if(query.length<2){notice('请输入至少两个字符');return;}
  if(query!==lastSearch){searchOffset=0;lastSearch=query;}
  const generation=++readGeneration,selected=account();
  const hits=await call('search',{account:selected,query,offset:searchOffset,limit:100});
  if(generation!==readGeneration||selected!==account())return;
  $('messages').replaceChildren();$('conversation-title').textContent='搜索“'+query+'”';$('page-count').textContent='第 '+(searchOffset+1)+' 起 · '+hits.length+' 条';$('search-paging').hidden=false;
  if(!hits.length)$('messages').append(el('p','没有符合条件的记录','empty'));
  for(const hit of hits){const button=el('button',undefined,'search-hit');button.append(el('strong',hit.conversation.label),el('div',hit.preview),el('small',date(hit.timestamp)+' · '+(hit.sender_label||hit.sender_id)));button.addEventListener('click',attempt(async()=>{current={type:'messages',conversation:hit.conversation};await loadPage({around:hit.msg_id});document.querySelector('[data-message-id="'+hit.msg_id+'"]')?.scrollIntoView({block:'center'});}));$('messages').append(button);}
}
function openInspector(title){$('inspector-title').textContent=title;$('inspector-content').replaceChildren();if(!$('inspector').open)$('inspector').showModal();return $('inspector-content');}
async function inspectRaw(item){const values=await call('observations',{account:account(),msg_id:item.msg_id});const content=openInspector('来源与版本 · '+item.msg_id);content.append(el('p','每个版本保留原生字段和采集信息；历史来源与媒体关联按原始资料展示。'),el('pre',JSON.stringify(values,null,2)));}
async function inspectMedia(item,voice){
  const links=await call('media',{account:account(),source_table:item.source_table,msg_id:item.msg_id,voice});const content=openInspector('媒体关联 · '+item.msg_id);
  if(!links.length)content.append(el('p','此消息没有可识别的媒体关联。原生内容仍可在来源与版本中查看。'));
  for(const link of links){
    const box=el('div',undefined,'media-item');box.append(el('strong',link.filename||link.media_type||'媒体'),el('p',link.verification+' · '+(link.note||''),'muted'));
    const asset=link.playable||link.primary||link.preview||link.candidate;
    if(asset&&asset.url){
      const mime=asset.detected_mime||'';let player;
      if(mime.startsWith('image/')&&mime!=='image/svg+xml'){player=el('img');player.alt=link.filename||'聊天图片';player.loading='lazy';}
      else if(mime.startsWith('video/')){player=el('video');player.controls=true;player.preload='metadata';}
      else if(mime.startsWith('audio/')){player=el('audio');player.controls=true;player.preload='metadata';}
      if(player){player.src=asset.url;box.append(player);}
      const download=el('a','下载媒体副本');download.href=asset.url;download.download=link.filename||asset.original_name||'media';box.append(download,el('div',asset.archive_relative||'','muted'));
    }else box.append(el('p','关联已保留，文件当前不可用。','muted'));
    if(link.voice_url){const audio=el('audio');audio.controls=true;audio.src=link.voice_url;box.append(audio);}
    if(!voice&&(link.media_type==='voice'||link.media_type==='audio'||/silk|语音/i.test((asset?.detected_mime||'')+' '+(link.filename||'')))){const convert=el('button','转换并播放语音');convert.addEventListener('click',attempt(()=>inspectMedia(item,true)));box.append(convert);}
    const detail=el('details');detail.append(el('summary','查看媒体来源与校验信息'),el('pre',JSON.stringify(link,null,2)));box.append(detail);content.append(box);
  }
}
function backupArgs(){return {repository:$('repository').value.trim()||undefined,point:$('point').value||undefined};}
async function listBackups(){
  const maintenance=await call('repository.status',backupArgs());$('repository-policy').textContent=(maintenance.rolling_enabled===false?'旧仓尚未启用滚动淘汰，请先固定需长期保留的点，再执行验证整理。':'')+'默认保留最近 '+maintenance.policy.recent+' 个成功点和全部固定点；每 '+maintenance.policy.compact_days+' 天自动验证整理。当前 '+number(maintenance.points)+' 个点，'+number(maintenance.expired)+' 个待退役。整理需临时空间，可在任务中取消。';
  const result=await call('backups',backupArgs());const selected=$('point').value;$('point').replaceChildren();const latest=el('option','最新备份点');latest.value='';$('point').append(latest);$('backup-points').replaceChildren();
  for(const item of result.points){
    const option=el('option',date(item.created)+' · '+item.id.slice(0,12)+(item.pinned?' · 已固定':''));option.value=item.id;$('point').append(option);
    const box=el('div',undefined,'backup-point');box.append(el('strong',date(item.created)+(item.pinned?' · 已固定':'')),el('div',number(item.files)+' 文件 · '+bytes(item.logical_bytes)+(item.complete_originals?' · 包含全部历史原始资料':''),'muted'),el('code',item.id));
    const actions=el('div',undefined,'actions');const choose=el('button','选择此备份点');choose.addEventListener('click',()=>{$('point').value=item.id;notice('已选择备份点 '+item.id.slice(0,12));});const pin=el('button',item.pinned?'取消固定':'固定备份点');pin.addEventListener('click',attempt(async()=>{await call('backup.pin',{...backupArgs(),point:item.id,pinned:!item.pinned});await listBackups();}));actions.append(choose,pin);box.append(actions);$('backup-points').append(box);
  }
  if([...$('point').options].some(option=>option.value===selected))$('point').value=selected;
  if(!result.points.length)$('backup-points').append(el('p','尚未创建备份点','empty'));
}
function renderJobs(values){
  const key=JSON.stringify(values);if(key===jobsKey)return;jobsKey=key;
  const container=$('jobs');container.replaceChildren();
  for(const value of values){
    const box=el('div',undefined,'job');const heading=el('div');heading.append(el('strong',(window.operationTitles||{})[value.operation]||value.operation),el('span',labels[value.state]||value.state,'badge'));box.append(heading,el('div',date(value.started||value.created)+(value.finished?' · '+(value.finished-(value.started||value.created)).toFixed(1)+' 秒':''),'muted'));
    if(value.error)box.append(el('p',value.error,'job-error'));
    if(value.progress)box.append(el('div','当前进度：'+JSON.stringify(value.progress),'muted'));
    const actions=el('div',undefined,'actions');if(['queued','running'].includes(value.state)){const cancel=el('button','取消任务');cancel.addEventListener('click',attempt(async()=>{await request('/api/cancel',{id:value.id});notice('已请求取消，将在安全检查点停止');}));actions.append(cancel);}
    const details=el('button','查看结果');details.addEventListener('click',attempt(async()=>{const job=await request('/api/job?id='+value.id);openInspector('任务结果').append(el('pre',JSON.stringify(job,null,2)));}));actions.append(details);box.append(actions);container.append(box);
  }
  if(!values.length)container.append(el('p','还没有操作任务','empty'));
}
function fillSettings(values){const form=$('settings-form');for(const name of ['poll_seconds','capture_seconds'])form.elements[name].value=values[name]||'';for(const [input,key] of [['media_minutes','media_seconds'],['full_minutes','full_verify_seconds'],['backup_minutes','backup_seconds']])form.elements[input].value=(values[key]||0)/60;for(const name of ['auto_capture','auto_backup'])form.elements[name].checked=!!values[name];}
async function saveSettings(event){
  event.preventDefault();try{const form=$('settings-form');const values={};for(const name of ['poll_seconds','capture_seconds'])values[name]=Number(form.elements[name].value);for(const [input,key] of [['media_minutes','media_seconds'],['full_minutes','full_verify_seconds'],['backup_minutes','backup_seconds']])values[key]=Number(form.elements[input].value)*60;for(const name of ['auto_capture','auto_backup'])values[name]=form.elements[name].checked;await call('settings',{settings:values});await refresh();notice('设置已保存');}catch(error){notice(error.message,true);}
}
document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',attempt(async()=>{show(button.dataset.page);if(button.dataset.page==='chats'&&!conversations.length)await loadConversations();if(button.dataset.page==='backups')await listBackups();})));
document.querySelectorAll('[data-op]').forEach(button=>button.addEventListener('click',attempt(async()=>{await call(button.dataset.op);await refresh();notice('操作已完成，可在任务中查看结果');})));
$('refresh').addEventListener('click',attempt(async()=>{accounts=[];await refresh();}));
$('account').addEventListener('change',attempt(async()=>{readGeneration++;conversationGeneration++;current={type:'messages',conversation:null};page=[];conversations=[];renderMetrics();$('messages').replaceChildren(el('p','请为当前账号选择会话','empty'));await loadConversations();}));
$('load-conversations').addEventListener('click',attempt(loadConversations));$('conversation-query').addEventListener('keydown',event=>{if(event.key==='Enter')attempt(loadConversations)();});
$('timeline').addEventListener('click',attempt(async()=>{current={type:'timeline',conversation:null};await loadPage();}));
$('latest').addEventListener('click',attempt(()=>loadPage()));$('older').addEventListener('click',attempt(()=>page.length?loadPage({before:cursor(page[0])}):loadPage()));$('newer').addEventListener('click',attempt(()=>page.length?loadPage({after:cursor(page[page.length-1])}):loadPage()));
$('jump-date').addEventListener('click',attempt(async()=>{const at=Date.parse($('at-date').value)/1000;if(!Number.isFinite(at))throw Error('请选择日期与时间');await loadPage({at:Math.floor(at)});}));
$('search').addEventListener('click',attempt(search));$('search-query').addEventListener('keydown',event=>{if(event.key==='Enter')attempt(search)();});
$('search-prev').addEventListener('click',attempt(async()=>{searchOffset=Math.max(0,searchOffset-100);await search();}));$('search-next').addEventListener('click',attempt(async()=>{searchOffset+=100;await search();}));
$('close-inspector').addEventListener('click',()=>$('inspector').close());
$('make-backup').addEventListener('click',attempt(async()=>{await call('backup',{...backupArgs(),pin:$('pin-new').checked});await listBackups();}));$('list-backups').addEventListener('click',attempt(listBackups));
$('compact-repository').addEventListener('click',attempt(async()=>{const result=await call('repository.compact',backupArgs());await listBackups();openInspector('仓库整理完成').append(el('pre',JSON.stringify(result,null,2)));}));
$('recover-repository').addEventListener('click',attempt(async()=>{const result=await call('repository.recover',backupArgs());await listBackups();openInspector('仓库切换恢复').append(el('pre',JSON.stringify(result,null,2)));}));
$('verify-backup').addEventListener('click',attempt(async()=>{const result=await call('backup.verify',backupArgs());openInspector('备份验证完成').append(el('pre',JSON.stringify(result,null,2)));}));
for(const operation of ['restore','export-portable'])$(operation).addEventListener('click',attempt(async()=>{const destination=$('destination').value.trim();if(!destination)throw Error('请填写新的目标目录');const result=await call(operation,{...backupArgs(),destination,reuse_immutable:$('reuse-immutable').checked});openInspector(operation==='restore'?'恢复与验证完成':'完整迁移包已导出').append(el('pre',JSON.stringify(result,null,2)));}));
$('refresh-jobs').addEventListener('click',attempt(refresh));$('full-capture').addEventListener('click',attempt(()=>call('capture',{full:true})));$('full-media').addEventListener('click',attempt(()=>call('media.scan',{full:true})));
$('settings-form').addEventListener('submit',saveSettings);
for(const [id,enabled] of [['enable-web',true],['disable-web',false]])$(id).addEventListener('click',attempt(async()=>{await request('/api/web',{enabled});await refresh();}));
$('refresh-health').addEventListener('click',attempt(refresh));
$('desktop-form').addEventListener('submit',async event=>{event.preventDefault();try{const values={};for(const key of ['launch_at_login','start_in_tray','close_to_tray','important_notifications'])values[key]=$('desktop-form').elements[key].checked;await call('desktop.settings',{settings:values});await refresh();notice('启动、托盘与通知设置已保存');}catch(error){notice(error.message,true);}});
$('create-shortcuts').addEventListener('click',attempt(async()=>{await call('desktop.shortcuts');notice('已创建“QQ 本地聊天档案”和“QQ 归档后台运行”快捷方式');}));
$('copy-diagnostics').addEventListener('click',()=>{const content=openInspector('诊断信息');content.append(el('p','包含本机路径和任务原因，不包含源密钥与聊天正文。'));const text=el('textarea');text.value=JSON.stringify({time:new Date().toISOString(),running:status.running,priority:status.capture?.priority,health:status.health,sources:status.source_details},null,2);text.readOnly=true;text.className='diagnostics-copy';content.append(text);const copy=el('button','复制到剪贴板');copy.addEventListener('click',attempt(async()=>{try{await navigator.clipboard.writeText(text.value);}catch(error){text.select();if(!document.execCommand('copy'))throw Error('请选中诊断文字后复制');}copy.textContent='已复制';}));content.append(copy);});
window.addEventListener('hashchange',()=>show(location.hash.slice(1)));
show(location.hash.slice(1));
attempt(async()=>{const info=await request('/api/capabilities');window.operationTitles=info.operations;$('capabilities').textContent=JSON.stringify(info,null,2);await refresh();if(location.hash==='#chats')await loadConversations();if(location.hash==='#backups')await listBackups();})();
setInterval(()=>attempt(refresh)(),5000);

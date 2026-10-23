import {api, mutation, startPolling} from './client.js?v=1';
import {pageSlice, renderPager} from './pagination.js?v=1';

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'home',filter:'unfinished',data:null,chat:null,task:null,chatDrafts:{},chatPolishing:new Set(),conversationQuery:'',rendered:false};
const viewNames = {home:'工作台',conversations:'会话与记忆',personas:'角色档案',activity:'执行记录',settings:'数字人设置'};
const statusNames = {open:'待处理',in_progress:'进行中',done:'已完成',needs_attention:'需要你决定',pending:'处理中',sent:'已发送',failed:'失败',uncertain:'待核对',auto:'自动回复',draft:'先留草稿',off:'不自动回复'};
const pages = Object.fromEntries(['tasks','conversations','personas','actions'].map(key=>[key,{page:1,size:10}]));
const listRequests = {tasks:0,actions:0};
const folds = new Map();
let personaQuery='', personaKind='all', activityStatus='all';
let toastTimer;
function toast(text){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').classList.add('show');toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),4500);}
function timeLabel(ts){if(!ts)return '';return new Date(typeof ts==='string'&&ts.includes('T')?ts:Number(ts)*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function chatName(id){return state.data.conversations.find(c=>c.chat_id===id)?.name || '未关联会话';}
function humanPauseLabel(c){const seconds=Math.ceil(((c?.human_pause_until||0)*1000-Date.now())/1000);return seconds>0?`人工接管中 · 自动回复暂停 ${seconds} 秒`:'人工接管暂停已结束';}
function displayContent(text){const v=String(text||'');if(v.startsWith('[图片]'))return '图片';if(v.startsWith('[表情包]'))return '表情包';return v;}
function empty(title, sub='新的工作事项会出现在这里。'){return `<div class="empty"><div class="empty-icon">✓</div><strong>${esc(title)}</strong><p>${esc(sub)}</p></div>`;}
function badge(status){return `<span class="badge ${esc(status)}">${esc(statusNames[status]||status)}</span>`;}
let stateRequest = 0;
function updateNotice(error=''){
  const notice=$('#workspaceNotice');
  notice.hidden=!error&&!!state.data?.status.automation_enabled;
  notice.classList.toggle('is-error',!!error);
  notice.textContent=error?(state.data?'工作台连接中断，正在显示上次成功获取的数据。请检查服务后点击刷新。':'暂时无法连接工作台，请确认本地服务已启动后刷新。'):state.data?'自动回复已暂停 · 新回复只生成草稿，你仍可以手动发送。':'';
  if(error){$('#connectionDot').className='';$('#connectionText').textContent='工作台连接中断';}
}
function refreshCurrentView(){
  if(!state.rendered||state.view==='home'||state.view==='activity')renderView();
  else if(state.view==='conversations'&&!state.chat)renderConversationList();
}
async function withBusy(button, action){
  if(button.disabled)return;
  button.disabled=true;button.setAttribute('aria-busy','true');
  try{return await action();}catch(error){toast(error.message);}
  finally{button.disabled=false;button.removeAttribute('aria-busy');}
}
async function loadState(render=true){
  const request=++stateRequest;
  let data;
  try{data=await api('/api/state');}catch(error){
    if(request===stateRequest){updateNotice(error);if(!state.data)$('#mainView').innerHTML=empty('暂时无法连接工作台','请确认本地服务已启动，再点击右上角刷新重试。');}
    throw error;
  }
  if(request!==stateRequest)return;
  state.data=data;updateNotice();
  if(!$('#toggleAutomation').hasAttribute('aria-busy'))$('#toggleAutomation').disabled=false;
  const {status,tasks,conversations,personas,actions}=state.data;
  $('#connectionDot').className=status.connection==='connected'?'connected':'';
  $('#connectionText').textContent={connected:'飞书已连接',starting:'正在启动',connecting:'正在连接',reconnecting:'正在重连',error:'连接异常'}[status.connection]||status.connection;
  $('#toggleAutomation').textContent=status.automation_enabled?'暂停自动回复':'恢复自动回复';
  $('.tiny-badge').textContent=status.active_calls?`处理 ${status.active_calls} 项中`:status.connection==='connected'?'已就绪':'待连接';
  const counts=state.data.task_counts||{};
  const cards=[['未完成待办',counts.unfinished??tasks.filter(t=>t.status!=='done').length,'待处理、进行中与需决定事项','☷','unfinished'],['需要你决定',counts.needs_attention??tasks.filter(t=>t.status==='needs_attention').length,'查看需要判断的事项','◇','needs_attention'],['建立联系',personas.filter(p=>p.kind==='person').length,`${conversations.filter(c=>c.kind==='group').length} 个群聊 · 查看角色档案`,'♧','personas'],['已完成',counts.done??tasks.filter(t=>t.status==='done').length,'查看已完成的待办','↗','done']];
  setHtml($('#stats'),cards.map(([label,value,note,icon,target])=>`<button type="button" class="stat" data-stat="${target}"><span class="stat-label">${label}</span><span class="stat-icon" aria-hidden="true">${icon}</span><span class="stat-value">${value}</span><span class="stat-note">${note}</span></button>`).join(''));
  if($('#humanPauseStatus')){const c=conversations.find(c=>c.chat_id===state.chat);$('#humanPauseStatus').textContent=c?.human_pause_until?' · '+humanPauseLabel(c):'';}
  document.querySelectorAll('[data-auto-switch]').forEach(el=>{const p=personas.find(p=>p.key===el.dataset.autoSwitch);if(p&&!el.disabled)el.checked=p.mode==='auto';});
  document.querySelectorAll('[data-auto-status]').forEach(el=>{const p=personas.find(p=>p.key===el.dataset.autoStatus);if(p)el.textContent=p.mode==='auto'?'开启':p.mode==='draft'?'仅草稿':'关闭';});
  if(render||!state.rendered) refreshCurrentView();
}
function renderView(){
  if(!state.data)return;
  state.rendered=true;
  document.body.dataset.view=state.view;
  document.querySelectorAll('.nav').forEach(button=>{if(button.dataset.view===state.view)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
  if(state.view!=='conversations')document.body.dataset.chatOpen='false';
  $('.nav.active')?.classList.remove('active');$(`.nav[data-view="${state.view}"]`).classList.add('active');
  $('#breadcrumb').textContent=viewNames[state.view];
  $('#pageTitle').textContent={home:'工作台',conversations:'会话与记忆',personas:'联系人与角色',activity:'执行记录',settings:'数字人设置'}[state.view];
  ({home:renderHome,conversations:renderConversations,personas:renderPersonas,activity:renderActivity,settings:renderSettings}[state.view])();
}
function setHtml(element,html){if(element.innerHTML!==html)element.innerHTML=html;}
function taskRow(t){return `<div class="task-row"><button class="task-dot ${t.status==='done'?'done':''}" data-done="${t.id}" aria-label="${t.status==='done'?'重新打开任务':'标记完成'}">${t.status==='done'?'✓':''}</button><div class="task-content"><button type="button" class="task-title" data-task="${t.id}">${esc(t.title)}</button><div class="task-meta"><span>${esc(chatName(t.chat_id))}</span><span>·</span><span>${esc(t.due_at||timeLabel(t.created_at))}</span>${t.priority==='high'?'<span class="badge high">优先处理</span>':''}${t.status==='needs_attention'?badge(t.status):''}</div></div><button class="button ghost" data-task="${t.id}">处理 →</button></div>`;}
async function loadList(key){
  const request=++listRequests[key], view=key==='tasks'?'home':'activity';
  const target=$(`#${key}List`), controls=$(`#${key}Pages`);
  if(!target)return;
  const status=key==='tasks'?state.filter:activityStatus;
  const params=new URLSearchParams({status,page:pages[key].page,page_size:pages[key].size});
  target.setAttribute('aria-busy','true');
  try{
    const data=await api(`/api/${key}/page?${params}`);
    if(request!==listRequests[key]||state.view!==view||!target.isConnected)return;
    pages[key].page=data.page;
    if(key==='tasks')state.pageTasks=data.items;
    setHtml(target,data.items.map(key==='tasks'?taskRow:activityRow).join('')||empty('当前分类没有记录','试试其他分类，新的记录会出现在这里。'));
    renderPager(controls,key,data);
    $(`#${key}LoadStatus`).textContent='';
  }catch(error){
    if(request===listRequests[key]&&target.isConnected)$(`#${key}LoadStatus`).textContent='加载失败，点击右上角刷新重试。'+error.message;
  }finally{if(request===listRequests[key])target.removeAttribute('aria-busy');}
}
function renderHome(){
  if(!$('#tasksList'))$('#mainView').innerHTML=`<div class="card"><div class="card-head"><h2>我的待办</h2><button class="button secondary" id="newTask">＋ 添加待办</button></div><div class="tabs" id="taskTabs" aria-label="待办状态"></div><p id="tasksLoadStatus" class="list-status" role="status">正在加载待办…</p><div id="tasksList"></div><div id="tasksPages"></div></div><div class="card"><div class="card-head"><h2>最近的联系</h2><button class="button ghost" data-view="conversations">全部会话 →</button></div><div id="recentContacts"></div></div><div id="homeError" class="hint danger-text" hidden></div>`;
  setHtml($('#taskTabs'),[['unfinished','未完成'],['open','待处理'],['in_progress','进行中'],['needs_attention','需决定'],['done','已完成'],['all','全部']].map(([v,n])=>`<button class="tab ${v===state.filter?'active':''}" data-filter="${v}" aria-pressed="${v===state.filter}">${n}</button>`).join(''));
  setHtml($('#recentContacts'),state.data.conversations.slice(0,4).map(conversationRow).join('')||empty('等待新的联系','连接飞书后，收到的会话会显示在这里。'));
  $('#homeError').hidden=!state.data.status.error;$('#homeError').textContent=state.data.status.error||'';
  loadList('tasks');
}
function conversationRow(c){return `<button type="button" class="conversation-row ${state.chat===c.chat_id?'selected':''}" data-chat="${esc(c.chat_id)}"><span class="avatar">${esc(c.kind==='group'?'群':c.name.slice(0,1))}</span><div><h3>${esc(c.name)}${c.kind==='group'?'<span class="badge group">群聊</span>':''}</h3><p>${esc(displayContent(c.last_message)||c.overview||'暂无消息')}</p></div>${c.open_tasks?`<span class="badge">${c.open_tasks} 项待办</span>`:''}</button>`;}
function renderConversationList(){
  const query=state.conversationQuery.trim().toLocaleLowerCase();
  const matches=state.data.conversations.filter(c=>`${c.name} ${c.overview||''}`.toLocaleLowerCase().includes(query));
  const data=pageSlice(matches,pages.conversations.page,pages.conversations.size);pages.conversations.page=data.page;
  renderPager($('#conversationPages'),'conversations',data);
  setHtml($('#conversationList'),data.items.map(conversationRow).join('')||empty(query?'没有找到匹配的会话':'暂无会话',query?'试试其他姓名或关键词。':'连接飞书后，收到的会话会显示在这里。'));
  $('#conversationCount').textContent=query?`${matches.length} / ${state.data.conversations.length} 个会话`:`${matches.length} 个会话`;
}
function renderConversations(){
  state.chat=null;document.body.dataset.chatOpen='false';
  $('#mainView').innerHTML=`<div class="card chat-list-card"><div class="card-head"><h2>消息</h2><span class="subtle" id="conversationCount"></span></div><div class="list-search"><input id="conversationSearch" type="search" aria-label="搜索联系人或群聊" placeholder="搜索联系人或群聊" value="${esc(state.conversationQuery)}"></div><p id="conversationLoadStatus" class="subtle" role="status"></p><div id="conversationList"></div><div id="conversationPages"></div></div>`;
  renderConversationList();
  $('#conversationSearch').addEventListener('input',e=>{state.conversationQuery=e.target.value;pages.conversations.page=1;renderConversationList();});
}
async function selectChat(id){
  if(state.view!=='conversations'){state.view='conversations';renderView();}
  const previousChat=$('#conversationMessages')?state.chat:null;
  const c=state.data.conversations.find(c=>c.chat_id===id);if(!c)return;
  state.chat=id;
  const loading=$('#conversationLoadStatus');
  if(loading)loading.textContent=`正在打开 ${c.name}…`;
  let d;
  try{d=await api(`/api/conversations/${encodeURIComponent(id)}`);}catch(error){if(state.chat===id){state.chat=previousChat;if(loading?.isConnected)loading.textContent='打开失败，请重新选择会话。';}throw error;}
  finally{if(d&&loading?.isConnected)loading.textContent='';}
  if(state.chat!==id||state.view!=='conversations')return;
  document.body.dataset.chatOpen='true';
  $('#mainView').innerHTML='<div id="conversationDetail"></div>';
  $('#conversationDetail').innerHTML=`<div class="card wechat-room"><div class="chat-room-head"><button class="icon-button" id="chatBack" aria-label="返回会话列表" title="返回会话列表"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m14 6-6 6 6 6"/></svg></button><span class="avatar">${esc(c.kind==='group'?'群':c.name.slice(0,1))}</span><div class="chat-room-title"><h2>${esc(c.name)}</h2><p>${c.kind==='group'?'群聊':'私聊'}<span id="humanPauseStatus">${c.human_pause_until?' · '+esc(humanPauseLabel(c)):''}</span></p></div>${c.persona?autoReplySwitch(c.persona):''}</div><details class="chat-context"><summary>会话概览与记忆 <span>查看背景</span></summary><div class="chat-context-body"><div class="row-flex"><strong>会话概览</strong><button class="button ghost" id="refreshOverview">更新概览</button></div><div class="overview">${esc(d.context.overview||'暂无概览，收到新消息后会持续更新。')}</div><div class="label-heading">有效记忆</div>${d.memories.length?d.memories.map(m=>`<div class="memory-item"><span>${esc(m.content)}</span><button data-forget="${m.id}">移除</button></div>`).join(''):'<p class="subtle">暂无有效记忆</p>'}</div></details><div class="messages" id="conversationMessages">${messagesHtml(d.messages,id)}</div><div class="chat-compose-area"><form class="inline-compose" id="conversationSend"><div id="conversationReply" contenteditable="true" role="textbox" aria-multiline="true" aria-label="回复当前会话" data-placeholder="输入消息…"></div><input id="chatImageInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden><div class="chat-compose-toolbar"><button type="button" class="button secondary" id="chatAttachImage">＋ 图片</button><span class="subtle">Ctrl + Enter 发送</span><button type="button" class="button secondary" id="conversationPolish">✳ AI 润色</button><button class="button primary" id="conversationSendButton">发送</button></div></form><p class="subtle" id="conversationPolishHint"></p></div></div>`;
  $('#chatBack').addEventListener('click',()=>{renderConversations();window.scrollTo(0,0);$('#conversationSearch').focus({preventScroll:true});});
  state.chatMessageSignature=JSON.stringify(d.messages);
  window.scrollTo(0,0);
  $('#conversationMessages').scrollTop=$('#conversationMessages').scrollHeight;
  const replyInput=$('#conversationReply');
  state.chatRichDrafts ||= {};state.chatUploading ||= new Set();
  if(state.chatRichDrafts[id])replyInput.innerHTML=state.chatRichDrafts[id];else replyInput.textContent=state.chatDrafts[id]||'';
  const saveDraft=()=>{state.chatRichDrafts[id]=replyInput.innerHTML;state.chatDrafts[id]=editorText(replyInput);};
  replyInput.addEventListener('input',saveDraft);
  replyInput.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&!e.isComposing&&e.key==='Enter'&&!$('#conversationSendButton').disabled){e.preventDefault();$('#conversationSend').requestSubmit($('#conversationSendButton'));}});
  let savedRange=null;
  const rememberRange=()=>{const selection=window.getSelection();if(selection.rangeCount&&replyInput.contains(selection.anchorNode))savedRange=selection.getRangeAt(0).cloneRange();};
  ['keyup','mouseup','input'].forEach(event=>replyInput.addEventListener(event,rememberRange));
  const updateBusy=()=>{const busy=state.chatPolishing.has(id)||state.chatUploading.has(id);$('#conversationPolish').disabled=busy;$('#conversationSendButton').disabled=busy;$('#chatAttachImage').disabled=state.chatUploading.has(id);};
  updateBusy();
  async function insertImages(files){
    if(state.chatUploading.has(id)){toast('图片上传中，请稍后');return;}
    const existing=editorParts(replyInput).filter(p=>p.type==='image').length;
    if(existing+files.length>6){toast('最多 6 张图片');return;}
    state.chatUploading.add(id);updateBusy();
    try{for(const file of files){
      if(!['image/png','image/jpeg','image/gif','image/webp'].includes(file.type)||file.size>10*1024*1024){throw Error('仅支持 10 MB 以内的 PNG、JPG、GIF、WebP');}
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      const asset=await api('/api/media/upload',mutation('POST',{data}));
      const figure=document.createElement('figure');figure.contentEditable='false';figure.className='compose-image';
      const img=document.createElement('img');img.src=asset.url;img.dataset.uploadId=asset.upload_id;img.alt='待发送图片';figure.append(img);
      const remove=document.createElement('button');remove.type='button';remove.dataset.removeImage='1';remove.setAttribute('aria-label','移除图片');remove.textContent='×';figure.append(remove);
      const target=replyInput.isConnected?replyInput:(state.chat===id&&state.view==='conversations'?$('#conversationReply'):null);
      if(target){
        if(target===replyInput&&savedRange&&replyInput.contains(savedRange.commonAncestorContainer)){savedRange.collapse(false);savedRange.insertNode(figure);savedRange.setStartAfter(figure);savedRange.collapse(true);window.getSelection().removeAllRanges();window.getSelection().addRange(savedRange);}
        else target.append(figure);
        target.append(document.createElement('br'));
        if(target===replyInput)saveDraft();else target.dispatchEvent(new Event('input',{bubbles:true}));
      }else{state.chatRichDrafts[id]=(state.chatRichDrafts[id]||'')+figure.outerHTML+'<br>';}
    }}catch(e){toast(e.message||'图片上传失败');}
    finally{state.chatUploading.delete(id);if(state.chat===id&&state.view==='conversations'&&$('#conversationSendButton')){$('#conversationSendButton').disabled=state.chatPolishing.has(id);$('#conversationPolish').disabled=state.chatPolishing.has(id);$('#chatAttachImage').disabled=false;}}
  }
  $('#chatAttachImage').addEventListener('click',()=>{rememberRange();$('#chatImageInput').click();});
  $('#chatImageInput').addEventListener('change',e=>{const files=Array.from(e.target.files);e.target.value='';insertImages(files);});
  replyInput.addEventListener('paste',e=>{
    e.preventDefault();rememberRange();
    const files=Array.from(e.clipboardData.files).filter(f=>f.type.startsWith('image/'));
    if(files.length){insertImages(files);return;}
    const text=e.clipboardData.getData('text/plain');const selection=window.getSelection();
    if(selection.rangeCount&&replyInput.contains(selection.anchorNode)){const range=selection.getRangeAt(0);range.deleteContents();const node=document.createTextNode(text);range.insertNode(node);range.setStartAfter(node);range.collapse(true);selection.removeAllRanges();selection.addRange(range);}else replyInput.append(document.createTextNode(text));saveDraft();
  });
  replyInput.addEventListener('dragover',e=>e.preventDefault());
  replyInput.addEventListener('drop',e=>{e.preventDefault();const files=Array.from(e.dataTransfer.files).filter(f=>f.type.startsWith('image/'));if(files.length)insertImages(files);});
  replyInput.addEventListener('click',e=>{const remove=e.target.closest('[data-remove-image]');if(remove){remove.closest('figure').remove();saveDraft();}});
  $('#conversationPolish').addEventListener('click',async()=>{
    const original=replyInput.innerHTML,text=editorText(replyInput);saveDraft();state.chatPolishing.add(id);updateBusy();$('#conversationPolishHint').textContent='润色中…';
    try{const j=await api(`/api/conversations/${encodeURIComponent(id)}/polish`,mutation('POST',{text}));const result=await waitJob(j.job_id);
      if(replyInput.isConnected&&replyInput.innerHTML===original){const images=Array.from(replyInput.querySelectorAll('figure'));replyInput.textContent=result.reply;for(const image of images){replyInput.append(document.createElement('br'),image);}saveDraft();$('#conversationPolishHint').textContent='已润色';}
      else{if(!replyInput.isConnected){const draft=document.createElement('div');draft.innerHTML=state.chatRichDrafts[id]||'';if(draft.innerHTML===original){const images=Array.from(draft.querySelectorAll('figure'));draft.textContent=result.reply;for(const image of images)draft.append(document.createElement('br'),image);state.chatRichDrafts[id]=draft.innerHTML;state.chatDrafts[id]=result.reply;toast('草稿已润色');}else toast('草稿已变更，未覆盖当前内容');}else toast('草稿已变更，未覆盖当前内容');}
    }catch(e){toast(e.message);}finally{state.chatPolishing.delete(id);if(state.chat===id&&state.view==='conversations'&&$('#conversationSendButton')){$('#conversationSendButton').disabled=state.chatUploading.has(id);$('#conversationPolish').disabled=state.chatUploading.has(id);}}
  });
  $('#conversationSend').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter,original=replyInput.innerHTML,parts=editorParts(replyInput);saveDraft();
    if(!parts.some(p=>p.type==='image'||p.text?.trim())){toast('请输入消息或添加图片');return;}b.disabled=true;
    try{const r=await api(`/api/conversations/${id}/send-rich`,mutation('POST',{parts,request_id:crypto.randomUUID()}));toast(statusNames[r.status]||r.status);
      if(r.status==='sent'&&state.chatRichDrafts[id]===original){state.chatDrafts[id]='';state.chatRichDrafts[id]='';if(replyInput.isConnected)replyInput.innerHTML='';}
      await loadState(false);await refreshChatMessages();
    }catch(e){toast(e.message);}finally{b.disabled=false;}
  });
  $('#refreshOverview').addEventListener('click',async e=>{const b=e.target;b.disabled=true;b.textContent='分析中…';try{const j=await api(`/api/conversations/${id}/overview`,{method:'POST'});await waitJob(j.job_id);await loadState(false);if(state.chat===id&&state.view==='conversations')await selectChat(id);toast('会话概览已更新');}catch(e){toast(e.message);b.disabled=false;b.textContent='更新概览';}});
}
async function refreshChatMessages(){
  const id=state.chat;if(state.view!=='conversations'||!id||state.chatRefreshing||!$('#conversationMessages'))return;
  state.chatRefreshing=true;
  try{const d=await api(`/api/conversations/${encodeURIComponent(id)}`);if(state.view!=='conversations'||state.chat!==id||!$('#conversationMessages'))return;
    const signature=JSON.stringify(d.messages);if(signature===state.chatMessageSignature)return;
    const box=$('#conversationMessages'),top=box.scrollTop,nearBottom=box.scrollHeight-box.clientHeight-top<60;
    box.innerHTML=messagesHtml(d.messages,id);state.chatMessageSignature=signature;
    box.scrollTop=nearBottom?box.scrollHeight:top;
  }finally{state.chatRefreshing=false;}
}
function editorParts(editor){
  const parts=[];const addText=text=>{if(!text)return;if(parts.at(-1)?.type==='text')parts.at(-1).text+=text;else parts.push({type:'text',text});};
  function walk(node){
    if(node.nodeType===Node.TEXT_NODE){addText(node.textContent);return;}
    if(node.nodeType!==Node.ELEMENT_NODE)return;
    if(node.matches('button'))return;
    if(node.matches('img[data-upload-id]')){parts.push({type:'image',upload_id:node.dataset.uploadId});return;}
    if(node.tagName==='BR'){addText('\n');return;}
    for(const child of node.childNodes)walk(child);
    if(['DIV','P','FIGURE'].includes(node.tagName)&&node!==editor)addText('\n');
  }
  for(const child of editor.childNodes)walk(child);
  if(parts.at(-1)?.type==='text')parts.at(-1).text=parts.at(-1).text.replace(/\n+$/,'');
  return parts.filter(p=>p.type==='image'||p.text);
}
function editorText(editor){return editorParts(editor).filter(p=>p.type==='text').map(p=>p.text).join('');}
function messagesHtml(messages,id){return messages.map(m=>{
  const parts=m.display_parts||[];
  const body=parts.length?parts.map((p,index)=>p.type==='image'?`<img loading="lazy" class="message-image ${p.sticker?'sticker-image':''}" src="/api/conversations/${encodeURIComponent(id)}/media/${encodeURIComponent(m.msg_id)}/${index}" alt="${p.sticker?'表情包':'图片'}" ${p.display_width?`style="width:${Math.min(600,Math.max(24,Number(p.display_width)))}px"`:''}>`:esc(p.text)).join(''):esc(displayContent(m.content));
  return `<div class="message ${m.direction==='out'?'out':''}"><div class="message-meta">${esc(m.sender_name||'我')} · ${esc(timeLabel(String(m.create_time)))}</div><div class="message-body">${body}</div></div>`;
}).join('')||'<p class="subtle">暂无消息</p>';}
const groupReplyNames={off:'不自动回复',all:'闲聊模式',at_me:'仅回复 @我',at_all:'仅回复 @全体'};
function groupReplySelect(p,form=false){return `<select ${form?'name="group_reply_policy"':`data-group-policy="${esc(p.key)}"`} aria-label="群聊回复方式">${Object.entries(groupReplyNames).map(([value,label])=>`<option value="${value}" ${(p.group_reply_policy||'off')===value?'selected':''}>${label}</option>`).join('')}</select>`;}
function autoReplySwitch(p){if(p.kind==='group')return groupReplySelect(p);return `<label class="auto-reply-switch"><input type="checkbox" role="switch" aria-label="${esc(p.name)} 自动回复" data-auto-switch="${esc(p.key)}" ${p.mode==='auto'?'checked':''}><span>自动回复</span><strong data-auto-status="${esc(p.key)}">${p.mode==='auto'?'开启':p.mode==='draft'?'仅草稿':'关闭'}</strong></label>`;}
function personaForm(p){return `<form class="form-grid persona-form" data-key="${esc(p.key)}"><label>角色 / 关系<input name="role" value="${esc(p.role)}" placeholder="例如：产品同事、项目负责人、客户"></label><label>你对这个人或群的了解<textarea name="description" rows="3" placeholder="负责什么、关注什么、沟通习惯、需要记住的背景">${esc(p.description)}</textarea></label><label>回复口吻<textarea name="reply_style" rows="2" placeholder="例如：直奔主题，少用表情；遇到排期先留待办">${esc(p.reply_style)}</textarea></label><label>自动处理方式${p.kind==='group'?groupReplySelect(p,true):`<select name="mode">${['auto','draft','off'].map(v=>`<option value="${v}" ${p.mode===v?'selected':''}>${statusNames[v]}</option>`).join('')}</select>`}</label><div><button class="button primary" type="submit">保存角色说明</button></div></form>`;}
function renderPersonas(){
  $('#mainView').innerHTML=`<div class="card"><div class="card-head"><h2>角色档案</h2><span class="subtle">展开档案编辑回复习惯</span></div><div class="list-toolbar"><input id="personaSearch" type="search" aria-label="搜索角色档案" placeholder="搜索姓名、角色或背景" value="${esc(personaQuery)}"><select id="personaKind" aria-label="角色类型">${[['all','全部类型'],['person','联系人'],['group','群聊']].map(([v,n])=>`<option value="${v}" ${personaKind===v?'selected':''}>${n}</option>`).join('')}</select></div><div id="personaRows">${state.data.personas.map(p=>`<div class="persona-row" data-persona-row="${esc(p.key)}"><span class="avatar">${esc(p.kind==='group'?'群':p.name.slice(0,1))}</span><div><div class="row-flex"><h3>${esc(p.name)}</h3>${autoReplySwitch(p)}</div><p>${esc(p.role||'未设置角色')}</p><details class="form-inline"><summary>编辑角色说明</summary><div class="section-pad">${personaForm(p)}</div></details></div></div>`).join('')}</div><div id="personaEmpty" hidden>${empty('没有匹配的角色','试试其他姓名或类型。')}</div><div id="personasPages"></div></div>`;
  $('#personaSearch').oninput=e=>{personaQuery=e.target.value;pages.personas.page=1;filterPersonas();};
  $('#personaKind').onchange=e=>{personaKind=e.target.value;pages.personas.page=1;filterPersonas();};
  filterPersonas();
}
function filterPersonas(){
  const query=personaQuery.trim().toLocaleLowerCase();
  const matches=state.data.personas.filter(p=>(personaKind==='all'||p.kind===personaKind)&&`${p.name} ${p.role} ${p.description}`.toLocaleLowerCase().includes(query));
  const data=pageSlice(matches,pages.personas.page,pages.personas.size);pages.personas.page=data.page;
  const visible=new Set(data.items.map(p=>p.key));
  document.querySelectorAll('[data-persona-row]').forEach(row=>row.hidden=!visible.has(row.dataset.personaRow));
  $('#personaEmpty').hidden=!!matches.length;
  renderPager($('#personasPages'),'personas',data);
}
function activityRow(a){return `<div class="activity-row"><div class="meta"><span>发给 ${esc(chatName(a.chat_id))} · ${esc(timeLabel(a.created_at))}</span>${badge(a.status)}</div><details class="activity-detail" data-section="action-${a.id}" ${folds.get(`action-${a.id}`)?'open':''}><summary>${esc(a.content||'（无文本内容）')}</summary><p>${esc(a.content)}</p>${a.error?`<p class="danger-text">${esc(a.error)}</p>`:''}</details></div>`;}
function renderActivity(){
  if(!$('#actionsList')){
    $('#mainView').innerHTML=`<div class="card"><div class="card-head"><h2>发送与执行记录</h2><select id="activityStatus" aria-label="执行状态">${[['all','全部状态'],['sent','已发送'],['pending','处理中'],['failed','失败'],['uncertain','待核对']].map(([v,n])=>`<option value="${v}" ${activityStatus===v?'selected':''}>${n}</option>`).join('')}</select></div><p id="actionsLoadStatus" class="list-status" role="status">正在加载执行记录…</p><div id="actionsList"></div><div id="actionsPages"></div></div>`;
    $('#activityStatus').onchange=e=>{activityStatus=e.target.value;pages.actions.page=1;loadList('actions');};
  }
  loadList('actions');
}
function settingSection(key,title,description,body,open=false){return `<details class="settings-section" data-section="${key}" ${(folds.get(key)??open)?'open':''}><summary><span><strong>${title}</strong><small>${description}</small></span><span class="fold-chevron" aria-hidden="true">⌄</span></summary><div class="settings-body">${body}</div></details>`;}
function renderSettings(){
  const status=state.data.status;
  $('#mainView').innerHTML=`<div class="settings-layout"><p class="view-description">按需要展开设置。回复设置与工作资料分别保存。</p><form id="settingsForm">
    ${settingSection('reply','回复习惯','设置数字人的角色、口吻和消息尾缀',`<div class="form-grid"><label>角色、工作习惯与回复原则<textarea id="ownRules" rows="5">${esc(status.own_rules)}</textarea></label><label>回复尾缀<input id="replySuffix" maxlength="200" value="${esc(status.reply_suffix)}" placeholder="留空则不添加尾缀"></label></div>`,true)}
    ${settingSection('automation','自动处理','控制人工接管和连续消息的等待时间',`<div class="form-grid"><label>人工接管等待（分钟）<input id="humanPauseMinutes" type="number" min="0" max="1440" step="0.1" required value="${esc((status.human_pause_seconds??120)/60)}"><span class="field-help">你手动回复后，自动回复暂停多久；0 表示关闭等待。</span></label><div class="field-pair"><label>消息停顿时间（秒）<input id="batchDelay" type="number" min="0.1" max="30" step="0.1" required value="${esc(status.batch_delay??1.5)}"></label><label>最长等待时间（秒）<input id="batchMaxWait" type="number" min="0.1" max="60" step="0.1" required value="${esc(status.batch_max_wait??5)}"></label></div><p class="field-help">等待对方说完再统一处理。最长等待时间不能短于停顿时间。</p></div>`)}
    <div class="settings-save"><span id="settingsSaveStatus" class="subtle" role="status">修改后点击保存</span><button type="submit" class="button primary">保存回复设置</button></div></form>
    ${settingSection('knowledge','工作资料','选择项目、整理职责，并指定可使用资料的会话','<div id="workKnowledge"><p class="subtle">正在加载工作资料…</p></div>')}
    ${settingSection('runtime','运行状态','查看连接、模型与当前处理情况',`<dl class="status-grid"><dt>飞书连接</dt><dd>${esc($('#connectionText').textContent)}</dd><dt>当前模型</dt><dd>${esc(status.model||'默认模型')}</dd><dt>排队消息</dt><dd>${status.queued_messages}</dd><dt>自动回复</dt><dd>${status.automation_enabled?'运行中':'已暂停，仅生成草稿'}</dd></dl><p class="field-help">${esc(status.rules)}</p>${status.error?`<p class="danger-text">${esc(status.error)}</p>`:''}`)}
  </div>`;
  $('#settingsForm').oninput=()=>$('#settingsSaveStatus').textContent='有未保存的修改';
  $('#settingsForm').addEventListener('submit',e=>{e.preventDefault();withBusy(e.submitter,async()=>{
    if(Number($('#batchMaxWait').value)<Number($('#batchDelay').value)){$('[data-section="automation"]').open=true;$('#batchMaxWait').focus();throw Error('最长等待时间不能短于消息停顿时间');}
    await api('/api/settings',mutation('PATCH',{own_rules:$('#ownRules').value,reply_suffix:$('#replySuffix').value,human_pause_seconds:Number($('#humanPauseMinutes').value)*60,batch_delay:Number($('#batchDelay').value),batch_max_wait:Number($('#batchMaxWait').value)}));
    $('#settingsSaveStatus').textContent='已保存';toast('回复设置已保存');await loadState(false);
  });});
  const knowledge=$('#workKnowledge');
  loadKnowledge(knowledge).catch(e=>{if(knowledge.isConnected)knowledge.textContent='资料加载失败：'+e.message;});
}

function knowledgeSources(p){return `<details class="knowledge-sources"><summary>${p.learned_at?'上次整理 '+esc(timeLabel(p.learned_at))+' · ':''}${(p.sources||[]).length} 条工作聊天来源</summary><label>聊天对应的项目目录<input data-field="archive_path" value="${esc(p.archive_path||p.path)}" placeholder="默认与项目目录相同"></label><p class="subtle">代码搬迁时，这里保留聊天记录中的旧目录，上方填写当前代码目录。</p><ul>${(p.sources||[]).map(s=>`<li>${s.kind==='active'?'未归档':'已归档'} · ${esc(s.time)} · ${esc(s.session_id)}</li>`).join('')||'<li>尚未从工作聊天整理。</li>'}</ul></details>`;}
function knowledgeProject(p){
  const chats=state.data.conversations;
  return `<details class="knowledge-project" data-project-id="${esc(p.id||crypto.randomUUID())}">
    <summary class="knowledge-project-head"><strong>${esc(p.name||'新项目')}</strong><span class="subtle">展开编辑</span></summary><div class="knowledge-project-body"><div class="project-actions"><button type="button" class="button ghost" data-remove-project>移除项目</button></div>
    <div class="knowledge-fields"><label>项目名称<input data-field="name" maxlength="100" required value="${esc(p.name)}" placeholder="例如：VehicleLogger"></label><label>代码与文档所在目录<input data-field="path" required value="${esc(p.path)}" placeholder="项目的本机绝对路径"></label></div>
    <label>项目别名 / 关键词<input data-field="aliases" value="${esc((p.aliases||[]).join('，'))}" placeholder="例如：DTU，设备日志，采集器"></label>
    <details class="knowledge-chats"><summary>用于回复哪些会话 · <span data-chat-count>${(p.chat_ids||[]).length}</span> 个</summary><p class="subtle">只在选中的会话中使用此项目的职责档案、代码和文档。不选会话时仅整理档案。</p><div class="knowledge-chat-tools"><input type="search" data-knowledge-chat-search placeholder="搜索联系人或群聊" aria-label="搜索此项目的适用会话"><button type="button" class="button ghost" data-select-chats="all">全选</button><button type="button" class="button ghost" data-select-chats="none">清空</button></div><div class="knowledge-chat-list">${chats.map(c=>`<label><input type="checkbox" data-knowledge-chat value="${esc(c.chat_id)}" ${(p.chat_ids||[]).includes(c.chat_id)?'checked':''}><span>${esc(c.name)}${c.kind==='group'?'（群聊）':''}</span></label>`).join('')||'<p class="subtle">暂无会话，建立联系后可在这里选择。</p>'}</div><p class="subtle" data-chat-empty hidden>没有匹配的会话</p></details>
    <label>我的职责与工作背景<textarea data-field="profile" rows="6" maxlength="6000" placeholder="点击“一键了解工作内容”从相关工作聊天整理，也可以直接填写或修改。">${esc(p.profile)}</textarea></label>
    ${knowledgeSources(p)}
  </div></details>`;
}

async function loadKnowledge(container){
  const data=await api('/api/knowledge');if(!container.isConnected)return;
  container.innerHTML=`<p class="field-help">从工作聊天整理职责；仅在你选择的会话中使用项目资料。</p>
    <div class="section-pad"><form id="knowledgeForm" class="form-grid"><details><summary>聊天来源目录（已归档与未归档）</summary><label>已归档聊天目录<input id="knowledgeArchive" required value="${esc(data.archive_dir)}" aria-label="已归档聊天目录"></label><label>未归档聊天目录<input id="knowledgeSessions" required value="${esc(data.sessions_dir)}" aria-label="未归档聊天目录"></label><p class="subtle">每个项目读取最近 30 条匹配聊天的近期问答，先筛选相关工作片段再整理，不包含工具输出或推理记录。</p></details>
    <div class="knowledge-toolbar"><button type="button" class="button secondary" id="discoverWork">从聊天发现项目</button><button type="button" class="button ghost" id="addWorkProject">＋ 手动添加项目</button></div><div id="knowledgeCandidates"></div>
    <div id="knowledgeProjects">${data.projects.map(knowledgeProject).join('')}</div>
    <div class="knowledge-toolbar"><button class="button secondary" type="submit" id="saveKnowledge">保存资料与档案</button><button class="button primary" type="button" id="learnWork">✦ 一键了解工作内容</button><button class="button secondary" type="button" id="cancelLearnWork" hidden>取消整理</button></div>
    <p id="knowledgeStatus" role="status" class="subtle"></p></form></div>`;
  let revision=data.revision;
  const status=container.querySelector('#knowledgeStatus');
  const form=container.querySelector('#knowledgeForm');
  const projects=container.querySelector('#knowledgeProjects');
  const removedProjects=new Map();
  const normalizePath=path=>{const value=String(path||'').trim().replaceAll('\\','/').replace(/\/+$/,'');return /^[a-z]:\//i.test(value)||value.startsWith('//')?value.toLowerCase():value;};
  const sourcePath=row=>normalizePath(row.querySelector('[data-field="archive_path"]').value||row.querySelector('[data-field="path"]').value);
  const projectRows=()=>[...projects.querySelectorAll('[data-project-id]')];
  function syncCandidates(){
    for(const button of container.querySelectorAll('[data-candidate-path]')){
      const selected=projectRows().some(row=>sourcePath(row)===button.dataset.candidatePath);
      button.setAttribute('aria-pressed',String(selected));button.classList.toggle('selected',selected);
      button.textContent=(selected?'✓ 已选 · ':'＋ ')+button.dataset.candidateLabel;
    }
  }
  function removeProject(row){removedProjects.set(sourcePath(row),row);row.remove();syncCandidates();status.textContent='项目已取消选择，保存后生效；也可以再次点击候选项目恢复。';}
  function addProject(p){
    if(projectRows().length>=12){toast('最多选择 12 个项目，请先取消不需要的项目');return;}
    const key=normalizePath(p.archive_path||p.path);
    if(key&&removedProjects.has(key)){projects.append(removedProjects.get(key));removedProjects.delete(key);}
    else projects.insertAdjacentHTML('beforeend',knowledgeProject(p));
    const added=projects.lastElementChild;if(added){added.open=true;added.querySelector('[data-field="name"]')?.focus();}
    syncCandidates();
    status.textContent='项目已选中，保存后生效。';
  }
  function updateChatCount(row){row.querySelector('[data-chat-count]').textContent=row.querySelectorAll('[data-knowledge-chat]:checked').length;}
  const payload=()=>({revision,archive_dir:container.querySelector('#knowledgeArchive').value.trim(),sessions_dir:container.querySelector('#knowledgeSessions').value.trim(),projects:[...container.querySelectorAll('[data-project-id]')].map(el=>({
    id:el.dataset.projectId,name:el.querySelector('[data-field="name"]').value.trim(),path:el.querySelector('[data-field="path"]').value.trim(),
    aliases:el.querySelector('[data-field="aliases"]').value.split(/[,，、\n]/).map(s=>s.trim()).filter(Boolean),
    chat_ids:[...el.querySelectorAll('[data-knowledge-chat]:checked')].map(c=>c.value),profile:el.querySelector('[data-field="profile"]').value,archive_path:el.querySelector('[data-field="archive_path"]').value.trim(),
  }))});
  async function save(){if(!form.reportValidity())throw Error('请填写项目名称和目录');const updated=await api('/api/knowledge',mutation('PUT',payload()));revision=updated.revision;for(const p of updated.projects){const row=[...container.querySelectorAll('[data-project-id]')].find(el=>el.dataset.projectId===p.id);if(row){row.querySelector('[data-field="profile"]').value=p.profile;row.querySelector('[data-field="path"]').value=p.path;row.querySelector('[data-field="archive_path"]').value=p.archive_path;}}return updated;}
  form.addEventListener('submit',async e=>{e.preventDefault();try{await save();status.textContent='资料与职责档案已保存，下次回复生效。';toast('工作资料已保存');}catch(e){status.textContent=e.message;}});
  container.onclick=e=>{
    const remove=e.target.closest('[data-remove-project]');if(remove){removeProject(remove.closest('[data-project-id]'));return;}
    const bulk=e.target.closest('[data-select-chats]');if(bulk){const row=bulk.closest('[data-project-id]');for(const check of row.querySelectorAll('[data-knowledge-chat]'))if(!check.closest('label').hidden)check.checked=bulk.dataset.selectChats==='all';updateChatCount(row);}
  };
  container.onchange=e=>{if(e.target.matches('[data-knowledge-chat]'))updateChatCount(e.target.closest('[data-project-id]'));if(e.target.matches('[data-field="path"],[data-field="archive_path"]'))syncCandidates();};
  container.oninput=e=>{
    if(!e.target.matches('[data-knowledge-chat-search]'))return;
    const row=e.target.closest('[data-project-id]');const query=e.target.value.trim().toLowerCase();let visible=0;
    for(const label of row.querySelectorAll('.knowledge-chat-list label')){label.hidden=!label.textContent.toLowerCase().includes(query);if(!label.hidden)visible++;}
    row.querySelector('[data-chat-empty]').hidden=visible>0||!query;
    row.querySelector('[data-select-chats="all"]').textContent=query?'全选结果':'全选';row.querySelector('[data-select-chats="none"]').textContent=query?'清空结果':'清空';
  };
  container.querySelector('#addWorkProject').onclick=()=>addProject({});
  container.querySelector('#discoverWork').onclick=async e=>{
    e.target.disabled=true;
    try{
      status.textContent='正在读取已归档与未归档的项目列表…';
      const candidates=await api('/api/knowledge/discover',mutation('POST',{archive_dir:container.querySelector('#knowledgeArchive').value.trim(),sessions_dir:container.querySelector('#knowledgeSessions').value.trim()}));
      const area=container.querySelector('#knowledgeCandidates');
      area.innerHTML=candidates.length?'<p class="subtle">点击选择项目，再次点击取消；整理时会进一步筛选工作内容。修改选择后请保存。</p>':'<p class="subtle">没有发现可读的聊天，可以手动添加项目。</p>';
      for(const p of candidates){const b=document.createElement('button');b.type='button';b.className='knowledge-candidate';b.dataset.candidatePath=normalizePath(p.path);b.dataset.candidateLabel=`${p.name} · ${p.count} 条聊天（已归档 ${p.archived_count} / 未归档 ${p.active_count}）${p.exists?'':' · 目录当前不可用'}`;b.title=p.path;
        b.onclick=()=>{const row=projectRows().find(el=>sourcePath(el)===b.dataset.candidatePath);if(row)removeProject(row);else addProject(p);};area.append(b);}
      syncCandidates();
      status.textContent=`发现 ${candidates.length} 个候选项目，添加后可选择适用会话。`;
    }catch(error){status.textContent=error.message;}finally{e.target.disabled=false;}
  };
  async function watch(id){
    const button=container.querySelector('#learnWork');
    button.disabled=true;form.querySelectorAll('input,textarea,button').forEach(el=>el.disabled=true);
    const cancel=container.querySelector('#cancelLearnWork');cancel.hidden=false;cancel.disabled=false;
    cancel.onclick=async()=>{cancel.disabled=true;cancel.textContent='正在取消…';try{await api(`/api/knowledge/learn/${encodeURIComponent(id)}/cancel`,mutation('POST',{}));}catch(e){status.textContent='取消未成功：'+e.message;cancel.disabled=false;cancel.textContent='取消整理';}};
    let shownSaved=0;
    const showProgress=async job=>{
      if(!container.isConnected)throw Error('已离开设置页');
      const p=job.result?.progress;
      const started=new Date(job.created_at).getTime();const seconds=Math.max(0,Math.floor((Date.now()-started)/1000));
      const elapsed=seconds>=60?`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`:`${seconds} 秒`;
      const parts=[`已运行 ${elapsed}`];
      if(p?.project_index)parts.push(`项目 ${p.project_index}/${p.project_total}：${p.project_name}`);
      if(p?.batch_total&&p.batch)parts.push(`批次 ${p.batch}/${p.batch_total}`);
      if(p)parts.push(`已保存 ${p.completed_projects||0} 个项目`);
      status.textContent=parts.join(' · ')+'\n'+(p?.message||'正在整理，旧版后台暂不提供批次进度')+'\n可以离开此页面，后台会继续处理；已完成项目会立即保存。';
      if((p?.completed_projects||0)>shownSaved){
        const latest=await api('/api/knowledge');if(!container.isConnected)throw Error('已离开设置页');
        for(const project of latest.projects){const row=projectRows().find(el=>el.dataset.projectId===project.id);if(!row)continue;row.querySelector('[data-field="profile"]').value=project.profile;const previous=row.querySelector('.knowledge-sources');const open=previous.open;previous.outerHTML=knowledgeSources(project);row.querySelector('.knowledge-sources').open=open;row.querySelectorAll('input,textarea,button').forEach(el=>el.disabled=true);}
        shownSaved=p.completed_projects;
      }
    };
    status.textContent='正在读取整理进度…';
    try{const result=await waitJob(id,showProgress);if(!container.isConnected)return;toast(result.answer);await loadKnowledge(container);}
    catch(e){if(container.isConnected){try{await loadKnowledge(container);}catch{status.textContent='整理状态读取失败：'+e.message;form.querySelectorAll('input,textarea,button').forEach(el=>el.disabled=false);cancel.hidden=true;syncCandidates();}}}
  }
  container.querySelector('#learnWork').onclick=async()=>{
    try{await save();const job=await api('/api/knowledge/learn',mutation('POST',{}));await watch(job.job_id);}catch(e){status.textContent=e.message;}
  };
  if(data.job&&['queued','running'].includes(data.job.status))watch(data.job.id);
  else if(data.job?.status==='failed')status.textContent='上次整理未完成：'+data.job.error;
  else if(data.job?.status==='done')status.textContent=JSON.parse(data.job.result||'{}').answer||'上次整理已完成。';
  else if(!data.projects.length)status.textContent='先从聊天发现项目，或手动填写项目目录。';
}
function showTask(id){
  const t=(state.pageTasks||[]).find(t=>t.id===Number(id))||state.data.tasks.find(t=>t.id===Number(id));if(!t)return;
  state.task=t;$('#taskTitle').textContent=t.title;$('#taskDetail').textContent=`${chatName(t.chat_id)} · ${statusNames[t.status]}${t.due_at?' · '+t.due_at:''}\n\n${t.details||'暂无补充说明'}`;$('#taskReply').value=t.draft_reply||'';$('#taskStatus').value=t.status;$('#taskPriority').value=t.priority;$('#taskPolishInstruction').value='';$('#taskPolishHint').textContent='';$('#taskSuffixPreview').textContent=state.data.status.reply_suffix?'发送时附加尾缀：'+state.data.status.reply_suffix:'当前未设置回复尾缀';$('#taskDialog').showModal();
}
async function waitJob(id,onUpdate){
  while(true){const j=await api(`/api/jobs/${id}`);if(j.status==='done')return j.result;if(j.status==='failed')throw Error(j.error);if(onUpdate)await onUpdate(j);await new Promise(r=>setTimeout(r,1500));}
}
function addBubble(text, kind='assistant'){let el=document.createElement('div');el.className=kind+'-bubble';el.textContent=text;$('#controlThread').append(el);$('#controlThread').scrollTop=$('#controlThread').scrollHeight;return el;}
function setAssistantOpen(open){
  $('#assistantPanel').hidden=!open;$('#assistantLauncher').setAttribute('aria-expanded',String(open));
  if(open){$('#assistantUnread').hidden=true;$('#controlText').focus();$('#controlThread').scrollTop=$('#controlThread').scrollHeight;}
  else $('#assistantLauncher').focus();
}
$('#assistantLauncher').addEventListener('click',()=>setAssistantOpen($('#assistantPanel').hidden));
$('#assistantClose').addEventListener('click',()=>setAssistantOpen(false));
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#assistantPanel').hidden&&!document.querySelector('dialog[open]')){e.preventDefault();setAssistantOpen(false);}});
async function command(text){
  addBubble(text,'user');const el=addBubble('正在整理上下文并处理…');el.classList.add('loading');$('#controlSubmit').disabled=true;
  try{const j=await api('/api/control',mutation('POST',{text}));const result=await waitJob(j.job_id);el.classList.remove('loading');el.textContent=result.answer||'已处理。';for(const action of result.actions||[]){const row=document.createElement('div');row.className='control-result'+(action.ok?'':' error');let label={send_message:'发送消息',create_task:'创建待办',complete_task:'完成任务',update_profile:'更新角色'}[action.type]||action.type;row.textContent=action.ok?`${label}：${action.data.status?statusNames[action.data.status]||action.data.status:'已完成'}`:`${label}：${action.error}`;el.append(row);}await loadState(state.view==='home'||state.view==='activity');}catch(e){el.classList.remove('loading');el.textContent='处理未完成：'+e.message;}finally{$('#controlSubmit').disabled=false;$('#controlThread').scrollTop=$('#controlThread').scrollHeight;if($('#assistantPanel').hidden)$('#assistantUnread').hidden=false;}
}
function changePage(key){
  if(key==='tasks'||key==='actions')loadList(key);
  else if(key==='conversations')renderConversationList();
  else filterPersonas();
}
document.addEventListener('click',e=>{
  const button=e.target.closest('[data-page-list]');if(!button||button.disabled)return;
  const key=button.dataset.pageList;if(!pages[key])return;
  pages[key].page=Number(button.dataset.page);changePage(key);
});
document.addEventListener('change',e=>{
  const key=e.target.dataset.pageSize;if(!pages[key])return;
  pages[key].size=Number(e.target.value);pages[key].page=1;changePage(key);
});
document.addEventListener('toggle',e=>{if(e.target.dataset.section)folds.set(e.target.dataset.section,e.target.open);},true);
document.addEventListener('invalid',e=>{let parent=e.target.parentElement;while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}},true);
document.addEventListener('input',e=>{
  if(e.target.matches('[data-field="name"]'))e.target.closest('[data-project-id]').querySelector('summary strong').textContent=e.target.value||'新项目';
});
document.addEventListener('click',async e=>{
  try{
  const stat=e.target.closest('[data-stat]');if(stat){if(stat.dataset.stat==='personas')state.view='personas';else{state.view='home';state.filter=stat.dataset.stat;pages.tasks.page=1;}renderView();$('#pageTitle').focus({preventScroll:true});return;}
  const image=e.target.closest('.message-image');if(image){let viewer=$('#imageViewer');if(!viewer){viewer=document.createElement('dialog');viewer.id='imageViewer';viewer.innerHTML='<button class="icon-button" data-close aria-label="关闭图片">×</button><img alt="图片预览">';document.body.append(viewer);}viewer.querySelector('img').src=image.src;viewer.showModal();return;}
  const nav=e.target.closest('button[data-view]');if(nav){if(!state.data)return;if(state.view!==nav.dataset.view){state.view=nav.dataset.view;renderView();window.scrollTo(0,0);}return;}
  const filter=e.target.closest('[data-filter]');if(filter){state.filter=filter.dataset.filter;pages.tasks.page=1;renderHome();}
  const task=e.target.closest('[data-task]');if(task)showTask(task.dataset.task);
  const done=e.target.closest('[data-done]');if(done){const t=(state.pageTasks||[]).find(t=>t.id===Number(done.dataset.done))||state.data.tasks.find(t=>t.id===Number(done.dataset.done));await withBusy(done,async()=>{await api(`/api/tasks/${t.id}`,mutation('PATCH',{status:t.status==='done'?'open':'done'}));await loadState();});}
  const chat=e.target.closest('[data-chat]');if(chat)await selectChat(chat.dataset.chat);
  const close=e.target.closest('[data-close]');if(close)close.closest('dialog').close();
  const forget=e.target.closest('[data-forget]');if(forget){await api(`/api/memories/${forget.dataset.forget}`,{method:'DELETE'});await selectChat(state.chat);}
  const cmd=e.target.closest('[data-command]');if(cmd)command(cmd.dataset.command);
  if(e.target.closest('#newTask')){$('#newTaskChat').innerHTML='<option value="">不关联会话</option>'+state.data.conversations.map(c=>`<option value="${esc(c.chat_id)}">${esc(c.name)}</option>`).join('');$('#newTaskDialog').showModal();}
  }catch(error){toast(error.message);}
});
document.addEventListener('change',async e=>{
  const group=e.target.closest('[data-group-policy]');
  if(group){const key=group.dataset.groupPolicy, policy=group.value;group.disabled=true;
    try{await api(`/api/personas/${encodeURIComponent(key)}`,mutation('PATCH',{group_reply_policy:policy}));await loadState(false);
      document.querySelectorAll('[data-group-policy]').forEach(el=>{if(el.dataset.groupPolicy===key)el.value=policy;});
      const form=document.querySelector(`.persona-form[data-key="${CSS.escape(key)}"]`);if(form?.elements.group_reply_policy)form.elements.group_reply_policy.value=policy;
      toast(groupReplyNames[policy]);
    }catch(err){group.value=state.data.personas.find(p=>p.key===key)?.group_reply_policy||'off';toast(err.message);}finally{group.disabled=false;}return;
  }
  const toggle=e.target.closest('[data-auto-switch]');if(!toggle)return;
  const key=toggle.dataset.autoSwitch, mode=toggle.checked?'auto':'off';toggle.disabled=true;
  try{await api(`/api/personas/${encodeURIComponent(key)}`,mutation('PATCH',{mode}));await loadState(false);
    document.querySelectorAll('[data-auto-switch]').forEach(el=>{if(el.dataset.autoSwitch===key)el.checked=mode==='auto';});
    document.querySelectorAll('[data-auto-status]').forEach(el=>{if(el.dataset.autoStatus===key)el.textContent=mode==='auto'?'开启':'关闭';});
    document.querySelectorAll('.persona-form').forEach(form=>{if(form.dataset.key===key)form.elements.mode.value=mode;});
    toast(mode==='auto'?'已开启此联系人的自动回复':'已关闭此联系人的自动回复，记忆和待办仍会更新');
  }catch(err){toggle.checked=!toggle.checked;toast(err.message);}finally{toggle.disabled=false;}
});
document.addEventListener('submit',async e=>{
  if(e.target.matches('.persona-form')){e.preventDefault();const fields=Object.fromEntries(new FormData(e.target));try{await api(`/api/personas/${encodeURIComponent(e.target.dataset.key)}`,mutation('PATCH',fields));toast('角色说明已保存，下次回复生效');await loadState(false);}catch(e){toast(e.message);}}
});
$('#controlForm').addEventListener('submit',e=>{e.preventDefault();const text=$('#controlText').value.trim();if(!text)return;$('#controlText').value='';command(text);});
$('#controlText').addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter')$('#controlForm').requestSubmit();});
$('#refresh').addEventListener('click',e=>withBusy(e.currentTarget,async()=>{await loadState();await refreshChatMessages();toast('已刷新，当前编辑内容已保留');}));
$('#toggleAutomation').addEventListener('click',e=>withBusy(e.currentTarget,async()=>{await api('/api/settings',mutation('PATCH',{automation_enabled:!state.data.status.automation_enabled}));await loadState();toast(state.data.status.automation_enabled?'自动回复已恢复':'已暂停自动发送，新回复保留为草稿');}));
$('#taskDone').addEventListener('click',async()=>{await api(`/api/tasks/${state.task.id}`,mutation('PATCH',{status:'done'}));$('#taskDialog').close();await loadState();});
$('#taskSave').addEventListener('click',async()=>{await api(`/api/tasks/${state.task.id}`,mutation('PATCH',{status:$('#taskStatus').value,priority:$('#taskPriority').value}));await loadState(false);toast('任务状态已保存');});
$('#taskPolish').addEventListener('click',async()=>{
  const taskId=state.task.id, original=$('#taskReply').value;
  const button=$('#taskPolish');button.disabled=true;$('#taskSendButton').disabled=true;
  $('#taskPolishHint').textContent='润色中…';
  try{const j=await api(`/api/tasks/${taskId}/polish`,mutation('POST',{text:original,instruction:$('#taskPolishInstruction').value}));const result=await waitJob(j.job_id);await loadState(false);
    if(state.task?.id===taskId){if(result.saved===false){$('#taskPolishHint').textContent=result.message+'。候选草稿：'+result.reply;}else if($('#taskReply').value===original){$('#taskReply').value=result.reply;$('#taskPolishHint').textContent='草稿已保存';}else{$('#taskPolishHint').textContent='AI 草稿已保存；已保留你正在修改的内容';}}
    toast('AI 草稿已生成，尚未发送');
  }catch(e){$('#taskPolishHint').textContent='润色未完成：'+e.message;toast(e.message);}finally{button.disabled=false;$('#taskSendButton').disabled=false;}
});
$('#taskReplyForm').addEventListener('submit',async e=>{e.preventDefault();let b=e.submitter;b.disabled=true;try{const r=await api(`/api/tasks/${state.task.id}/reply`,mutation('POST',{content:$('#taskReply').value,request_id:crypto.randomUUID()}));toast(statusNames[r.status]||r.status);if(r.status==='sent')$('#taskDialog').close();await loadState();}catch(e){toast(e.message);}finally{b.disabled=false;}});
$('#newTaskForm').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/tasks',mutation('POST',{title:$('#newTaskTitle').value,details:$('#newTaskDetails').value,chat_id:$('#newTaskChat').value,priority:'normal'}));$('#newTaskDialog').close();e.target.reset();await loadState();toast('待办已添加');}catch(e){toast(e.message);}});
$('#date').textContent=new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
api('/api/control/history').then(jobs=>{for(const j of jobs){addBubble(j.input,'user');if(j.status==='done'){const r=JSON.parse(j.result);const summary=(r.actions||[]).map(a=>`${a.type}：${a.ok?(statusNames[a.data?.status]||'已完成'):a.error}`).join('\n');addBubble((r.answer||'已处理')+(summary?'\n\n'+summary:''));}else if(j.status==='failed')addBubble('处理未完成：'+j.error);else addBubble('这条指令正在后台处理，可稍后刷新查看结果。');}}).catch(()=>{});
startPolling(async()=>{
  await loadState();
  await refreshChatMessages();
},{enabled:()=>!document.hidden,onError:updateNotice});

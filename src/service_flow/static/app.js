
const $=id=>document.getElementById(id);let role='customer',token='',cid=null,current=null,orders=[],demo=true,pendingToken='',busy=false,lastSignature='',generation=0,supervisor=false;
const states={bot:'自动服务中',waiting_human:'等待人工',human:'人工处理中',resolved:'已结案'};
function toast(text){$('toast').textContent=text;$('toast').classList.remove('hide');setTimeout(()=>$('toast').classList.add('hide'),4500)}
async function api(path,options={}){const r=await fetch(path,{...options,headers:{'Content-Type':'application/json',Authorization:'Bearer '+token,...options.headers}});let data;try{data=await r.json()}catch{throw Error('服务响应异常')}if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'输入格式有误，请检查后重试');return data}
function post(path,data={}){return api(path,{method:'POST',body:JSON.stringify(data)})}
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n}
async function login(){generation++;cid=null;current=null;lastSignature='';$('messages').replaceChildren(el('div','请选择会话或新建会话。','empty'));$('state').textContent='准备就绪';$('banner').textContent='请选择会话';$('details').replaceChildren();if(demo){const r=await post('/api/demo/login',{role,identity:$('identity').value});token=r.token}else{token=$('credential').value;if(!token)return;const me=await api('/api/me');if(me.role!==role)throw Error('凭据角色与当前工作台不匹配');const option=el('option',me.id);option.value=me.id;$('identity').replaceChildren(option)}supervisor=(await api('/api/me')).permissions?.includes('supervisor')||false;if(role==='customer'){orders=await api('/api/orders');$('order').replaceChildren(...orders.map(o=>{let n=el('option',o.order_id+' · '+o.product);n.value=o.order_id;return n}))}await refreshList();const saved=sessionStorage.getItem('sf:'+role+':'+$('identity').value);if(saved){try{await select(saved)}catch{sessionStorage.removeItem('sf:'+role+':'+$('identity').value)}}}
async function switchRole(next){role=next;for(const id of ['metrics','filters','operations'])$(id).classList.toggle('hide',role!=='staff');$('customerTab').classList.toggle('active',role==='customer');$('staffTab').classList.toggle('active',role==='staff');$('orderBox').classList.toggle('hide',role==='staff');$('suggestions').classList.toggle('hide',role==='staff');$('listTitle').textContent=role==='staff'?'待处理队列':'我的会话';$('identity').replaceChildren(...(role==='customer'?['C-001','C-002','C-003','C-004']:['S-001','S-002']).map(v=>{const o=el('option',v);o.value=v;return o}));await login()}
async function refreshList(){if(role==='staff')await refreshOperations();const g=generation;const rows=await api(role==='staff'?'/api/staff/queue?status='+$('statusFilter').value+'&queue='+$('queueFilter').value:'/api/conversations');if(g!==generation)return;$('list').replaceChildren();if(!rows.length)$('list').append(el('div',role==='staff'?'目前没有待处理工单':'还没有售后会话','pad'));for(const row of rows){const n=el('button',undefined,'item'+(cid===row.id?' selected':''));n.append(el('strong',row.order_id),el('small',(states[row.state]||row.state)+(row.priority==='urgent'?' · 紧急':'')),el('small',row.reason||row.topic||'新的售后会话'));n.onclick=()=>select(row.id).catch(e=>toast(e.message));$('list').append(n)}}
async function select(id){sessionStorage.setItem('sf:'+role+':'+$('identity').value,id);cid=id;lastSignature='';await refresh();await refreshList()}
async function refresh(){if(!cid)return;const id=cid,g=generation;const row=await api('/api/conversations/'+id);if(id!==cid||g!==generation)return;current=row;const sig=JSON.stringify(row);if(role==='staff')await refreshAudit(id);if(sig===lastSignature)return;lastSignature=sig;render(row)}
function render(row){renderCaseControls(row);$('chatTitle').textContent=row.order_id+' · 售后会话';$('state').textContent=states[row.state];$('state').classList.toggle('warn',row.state==='waiting_human');$('banner').classList.toggle('urgent',row.priority==='urgent');$('banner').textContent=row.state==='waiting_human'?'已转人工，自动处理已暂停。可以继续补充信息，客服接管后会在此回复。':row.state==='human'?'客服 '+row.assignee+' 已接管。新的消息将发送给客服。':row.state==='resolved'?'会话已结案。需要其他帮助时，可以新建会话。':'自动服务已就绪。我们会结合本会话历史理解你的追问。';const box=$('messages'),nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<80;box.replaceChildren();for(const m of row.messages){let n=el('div',undefined,'bubble '+m.role);n.append(el('div',({user:role==='staff'?'客户':'你',assistant:'服务助手',staff:'人工客服',system:'处理进度'})[m.role]+' · '+new Date(m.created_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),'by'),el('div',m.content));for(const c of m.metadata.citations||[])n.append(el('div','依据：'+(c.source||c.title),'citation'));if(role==='customer'&&m.metadata.confirmation_token&&m.metadata.confirmation_status==='pending'&&row.state==='bot'){const b=el('button','查看并确认退款');b.onclick=()=>{pendingToken=m.metadata.confirmation_token;$('confirmText').textContent=m.content;$('confirmation').showModal()};n.append(b)}box.append(n)}if(!row.messages.length)box.append(el('div','描述你的问题，我们从这里开始。','empty'));if(nearBottom||!row.messages.length)box.scrollTop=box.scrollHeight;$('claim').classList.toggle('hide',role!=='staff'||row.state!=='waiting_human');for(const id of ['resume','resolve'])$(id).classList.toggle('hide',role!=='staff'||row.state!=='human'||row.assignee!==$('identity').value);$('resume').disabled=row.priority==='urgent';$('send').disabled=busy||row.state==='resolved'||(role==='staff'&&(row.state!=='human'||row.assignee!==$('identity').value));$('details').replaceChildren();for(const [label,value] of [['订单编号',row.order_id],['产品',row.order.product],['实付金额','¥'+row.order.paid_amount.toFixed(2)],['订单状态',row.order.status==='refunded'?'已完成本地退款':'已签收'],['客户',row.customer_id],['处理状态',states[row.state]],['优先级',row.priority==='urgent'?'紧急安全问题':'普通'],['当前客服',row.assignee||'尚未分配'],['工单编号',row.ticket_id||'未创建'],['转接原因',row.reason||'—'],...(row.case?[['处理队列',queueNames[row.case.queue]],['工单状态',caseNames[row.case.status]],['首次响应截止',new Date(row.case.response_due*1000).toLocaleString()],['解决截止',new Date(row.case.resolve_due*1000).toLocaleString()],['超时状态',row.case.response_breached||row.case.resolution_breached?'已超期 · 已通知主管':'时限内']]:[])]){let n=el('div',undefined,'detail');n.append(el('small',label),el('strong',value));$('details').append(n)}$('details').append(el('div','会话中的订单保持固定。人工接管期间不再执行自动退款或排障。','note'))}
async function send(){if(!cid)return toast('请先选择或新建会话');const text=$('message').value.trim();if(!text)return;if(busy)return;busy=true;$('send').disabled=true;try{if(role==='staff')await caseAction('reply',text);else await post('/api/conversations/'+cid+'/messages',{request_id:crypto.randomUUID(),message:text});$('message').value='';await refresh();$('messages').scrollTop=$('messages').scrollHeight;await refreshList()}catch(e){toast(e.message)}finally{busy=false;if(current)render(current)}}
function bind(id,fn){$(id).onclick=()=>Promise.resolve().then(fn).catch(e=>toast(e.message))}bind('customerTab',()=>switchRole('customer'));bind('staffTab',()=>switchRole('staff'));$('identity').onchange=()=>login().catch(e=>toast(e.message));bind('loginToken',login);bind('newChat',async()=>{const row=await post('/api/conversations',{order_id:$('order').value});await select(row.id)});bind('send',send);bind('claim',async()=>{await post('/api/staff/conversations/'+cid+'/claim');await refresh();await refreshList()});for(const action of ['resume','resolve'])bind(action,async()=>{const note=$('message').value.trim();if(!note)return toast('请在输入框填写处理结论，再执行此操作');await caseAction(action,note);$('message').value='';await refresh();await refreshList()});$('cancelConfirm').onclick=()=>$('confirmation').close();bind('doConfirm',async()=>{$('doConfirm').disabled=true;try{await post('/api/conversations/'+cid+'/refund/confirm',{token:pendingToken});$('confirmation').close();await refresh()}finally{$('doConfirm').disabled=false}});document.querySelectorAll('[data-text]').forEach(b=>b.onclick=()=>{$('message').value=b.dataset.text;$('message').focus()});$('message').onkeydown=e=>{if(e.ctrlKey&&e.key==='Enter')send()};

const queueNames={technical:'技术支持',billing:'退款与账务',safety:'安全专席'};
const caseNames={open:'待接管',in_progress:'处理中',pending_customer:'等待客户补充',resolved:'已解决'};
async function caseAction(action,message){
  if(!current?.case)throw Error('请先选择工单');
  const body={action,message,expected_version:current.case.version};
  if(action==='transfer')body.target=$('transferTarget').value;
  if(action==='resolve')body.resolution_code=$('resolutionCode').value;
  if(action==='resume')body.resolution_code='return_to_bot';
  try{return await post('/api/staff/conversations/'+cid+'/case-action',body)}
  catch(e){await refresh();throw e}
}
function renderCaseControls(row){
  const owns=role==='staff'&&row.state==='human'&&row.assignee===$('identity').value;
  for(const id of ['staffOptions','waitCustomer','transfer'])$(id).classList.toggle('hide',!owns);
  $('reopen').classList.toggle('hide',!(role==='staff'&&supervisor&&row.case?.status==='resolved'));
}
for(const [id,action] of [['waitCustomer','wait_customer'],['transfer','transfer'],['reopen','reopen']]){
  bind(id,async()=>{const note=$('message').value.trim();if(!note)return toast('请填写处理说明');await caseAction(action,note);$('message').value='';await refresh();await refreshList()});
}
$('queueFilter').onchange=()=>refreshList().catch(e=>toast(e.message));
$('statusFilter').onchange=()=>refreshList().catch(e=>toast(e.message));
async function refreshOperations(){
  const [stats,events]=await Promise.all([api('/api/staff/overview'),api('/api/staff/outbox')]);
  if(role!=='staff')return;
  $('metrics').replaceChildren();
  for(const [label,value] of [['待接管',stats.cases.open||0],['等待客户',stats.cases.pending_customer||0],['超时记录',stats.sla_breaches],['失败待处理',stats.outbox.dead||0]]){
    const card=el('div',undefined,'metric');card.append(el('small',label),el('strong',String(value)));$('metrics').append(card);
  }
  $('outboxList').replaceChildren();
  for(const event of events.slice(0,8)){
    const item=el('div',undefined,'audit-item');item.append(el('div',event.event_type),el('small',({pending:'等待投递',processing:'投递中',delivered:'已送达通知中心',dead:'多次失败，等待主管处理'})[event.status]+' · 尝试 '+event.attempts+' 次'));
    if(event.status==='dead'&&supervisor){const retry=el('button','重试通知');retry.onclick=()=>post('/api/staff/outbox/'+event.id+'/retry').then(refreshOperations).catch(e=>toast(e.message));item.append(retry)}
    $('outboxList').append(item);
  }
}
async function refreshAudit(id){
  const entries=await api('/api/staff/conversations/'+id+'/audit');if(id!==cid||role!=='staff')return;
  const names={opened:'工单创建',claim:'客服接管',reply:'客服回复',wait_customer:'等待客户补充',transfer:'工单转交',resolve:'工单解决',resume:'恢复自动服务',reopen:'主管重开',customer_replied:'客户补充',safety_escalated:'安全升级',response_breached:'首次响应超时',resolution_breached:'解决时限超时'};
  $('auditList').replaceChildren();
  for(const entry of entries.slice(-12).reverse()){
    const item=el('div',undefined,'audit-item');item.append(el('div',(names[entry.action]||entry.action)+' · '+entry.actor),el('small',entry.note+' · '+new Date(entry.created_at*1000).toLocaleString()));$('auditList').append(item);
  }
}

fetch('/health').then(r=>r.json()).then(async h=>{demo=h.demo_mode;$('health').textContent='● 服务在线 · '+(h.router==='rules'?'规则路由':'模型路由');$('tokenBox').classList.toggle('hide',demo);$('mode').textContent=demo?'演示模式 · 合成订单 · 无真实资金交易':'凭据认证模式 · 本地退款账本';await switchRole('customer')}).catch(e=>toast('连接失败：'+e.message));setInterval(()=>{if(token&&!busy){refresh().catch(()=>{});refreshList().catch(()=>{})}},3000);

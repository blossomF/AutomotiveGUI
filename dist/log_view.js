/* Online and offline viewers share presentation and the legacy log contract. */
(function(root){
'use strict';
const num=(v,d=0)=>v===null||v===undefined||v===''?d:(Number.isFinite(Number(v))?Number(v):d);
const round=(v,n=3)=>Math.round((v+Number.EPSILON)*10**n)/10**n;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusNames={done:'已完成',running:'运行中',aborted:'未完成',error:'执行出错',timeout:'已超时',cancelled:'已取消',interrupted:'异常结束',unknown:'状态未知'};
const reasons={complete:'任务完成',failed:'任务未完成',interrupted:'任务被中断',cancelled:'用户取消任务',error:'执行异常',timeout:'超过任务时限',max_steps:'达到最大操作步数',no_progress:'连续操作未取得进展',unknown:'未记录明确结果'};
const phases={setup:'准备',perceive:'观察屏幕',route:'选择路径',decide:'决策',model:'模型请求',act:'执行',verify:'检查结果',recovery:'恢复',finish:'任务总结',wait:'等待',xml:'控件树',glm_request:'请求原文',glm_response:'响应原文',glm_error:'模型错误',guard:'限制检查',skill:'任务指南',input:'文字输入',ad:'启动弹窗',roll:'调整滚轮',error:'错误'};
const kinds={initial_app_route:'选择启动应用与指南',decide:'选择下一步操作',reflect:'检查操作效果',chat:'通用模型请求'};
const kindName=k=>kinds[k]||(String(k).endsWith('_json_retry')?(kinds[String(k).slice(0,-11)]||String(k).slice(0,-11))+'（格式重试）':k);
function token(ev){
 const t=ev.tokens;
 if(t&&typeof t==='object')return {input:Math.trunc(num(t.input??t.prompt_tokens)),output:Math.trunc(num(t.output??t.completion_tokens))};
 if(typeof t==='number')return {input:Math.trunc(t),output:0};
 if('in_tok'in ev||'out_tok'in ev)return {input:Math.trunc(num(ev.in_tok)),output:Math.trunc(num(ev.out_tok))};
 return {input:Math.trunc(num(ev.model_tokens)),output:0};
}
function project(events,sid){
 const match=String(sid).match(/-(\d{8})-(\d{6})$/);
 let start=null;
 if(match){const d=match[1],t=match[2];start=new Date(+d.slice(0,4),+d.slice(4,6)-1,+d.slice(6,8),+t.slice(0,2),+t.slice(2,4),+t.slice(4,6));}
 const task=events.find(e=>typeof e.task==='string'&&e.task.trim())?.task??sid.replace(/-\d{8}-\d{6}$/,'');
 const title=start?`${match[1].slice(4,6)}-${match[1].slice(6,8)} ${match[2].slice(0,2)}:${match[2].slice(2,4)}:${match[2].slice(4,6)} ${task}`:task;
 const counts={},groups=new Map(),steps=[];let finish=null,snapshot=null,previous=null,lastStep=0;
 events.forEach((ev,idx)=>{
  const phase=ev.phase||'log',step=Number.isInteger(ev.step)?ev.step:lastStep,t=num(ev.t);lastStep=step;
  counts[step]=(counts[step]||0)+1;
  const metrics=Object.fromEntries(Object.entries(ev).filter(([k])=>!['t','wall','step','phase','msg'].includes(k)));
  const wall=ev.wall||(start?new Date(+start+t*1000).toTimeString().slice(0,8):null);
  const modelMs=ev.latency_ms??ev.model_ms;
  steps.push({idx,step,substep:counts[step],wall_time:wall,action:{action:phase,summary:ev.msg||'',explain:ev.msg||'',...metrics},
   at_time:t,gap_time:previous===null?null:round(Math.max(0,t-previous)),inference_time:modelMs==null?null:round(num(modelMs)/1000),
   execution_time:ev.duration_ms!=null&&phase==='act'?round(num(ev.duration_ms)/1000):null,
   duration_time:ev.duration_ms==null?null:round(num(ev.duration_ms)/1000),total_time:null,tokens:token(ev),image:null,evidence:null,noop:false});
  previous=t;if(ev.usage_total&&typeof ev.usage_total==='object')snapshot=ev.usage_total;
  if(phase==='finish'){finish=ev;return;}
  if(phase==='model'&&ev.state!=='end')return;
  if(!groups.has(step))groups.set(step,{modern:[],response:new Map(),fallback:[]});
  const group=groups.get(step);
  if(phase==='model'&&ev.state==='end')group.modern.push(ev);
  else if(phase==='glm_response')group.response.set(String(ev.call_id??idx),ev);
  else if(['decide','route'].includes(phase)&&['tokens','in_tok','out_tok','model_tokens'].some(k=>k in ev))group.fallback.push(ev);
 });
 let legacyTokens=0,legacyCalls=0;
 groups.forEach(g=>{const source=g.modern.length?g.modern:g.response.size?[...g.response.values()]:g.fallback;source.forEach(e=>{const t=token(e);legacyTokens+=t.input+t.output;legacyCalls+=Math.trunc(num(e.calls??e.glms,1));});});
 const usage={...(finish?.usage||snapshot||{})};
 const totalTokens=Math.trunc(num(usage.total_tokens,legacyTokens));usage.calls??=legacyCalls;usage.total_tokens??=totalTokens;
 const known='usage_reports'in usage?num(usage.usage_reports)>0:('total_tokens'in (finish?.usage||snapshot||{})||events.some(e=>['tokens','in_tok','out_tok','model_tokens'].some(k=>k in e)));
 const elapsed=num(finish?.elapsed??finish?.t??previous);
 const graphSteps=Math.trunc(num(finish?.steps,new Set(steps.filter(s=>s.step>0).map(s=>s.step)).size));
 const reason=String(finish?.msg||'');
 const status=finish?(finish.success??(reason==='complete')?'done':({interrupted:'cancelled',cancelled:'cancelled',error:'error',timeout:'timeout'}[reason]||'aborted')):'unknown';
 return {task_dir:sid,session_id:sid,task,title,status,reason,complete:!!finish,usage,routes:finish?.routes||{},
  stats:{total_steps:graphSteps,total_time:round(elapsed,1),total_tokens:totalTokens,usage_known:known,usage_complete:!!(finish&&(finish.usage_complete??usage.usage_complete??known)),avg_step:graphSteps?round(elapsed/graphSteps,2):null},
  steps,cursor:events.length-1,event_count:events.length};
}
function summary(a){
 const p=a.action,msg=a.summary||a.explain||'';
 if(p==='finish')return reasons[msg]||msg||'任务结束';
 if(p==='model')return (kindName(a.request_kind)||'模型请求')+'：'+msg;
 if(p==='glm_request'||p==='glm_response')return (p==='glm_request'?'发送模型请求：':'收到模型响应：')+(kindName(a.request_kind)||'决策');
 if(p==='xml')return a.status==='ok'?'已获取控件树（'+num(a.chars)+' 字符）':a.status==='empty'?'控件树内容为空':'控件树获取失败（'+(a.status||'状态未知')+'）';
 if(p==='decide'){
  const match=/^([A-Z_]+)\s*(.*?)\s*—\s*(.*)$/.exec(msg);
  const names={CLICK:'点击',LONG_PRESS:'长按',TYPE:'输入',SCROLL:'滚动',ROLL_TO:'调整滚轮',DRAG:'拖动',BACK:'返回',HOME:'回到桌面',LAUNCH:'启动应用',SKILL:'加载指南',WAIT:'等待',COMPLETE:'完成任务',FAIL:'结束失败任务'};
  if(match)return '模型决定'+(names[match[1]]||match[1])+(match[2]?' '+match[2]:'')+(match[3]?'：'+match[3]:'');
 }
 if(p==='act'){
  const m=/^OK tap\((\d+),(\d+)\)/.exec(msg);
  if(m)return `ADB 已执行点击（${m[1]}，${m[2]}），效果由后续观察确认`;
  if(msg==='OK back')return 'ADB 已执行返回命令';
  if(msg==='OK home')return 'ADB 已执行回到桌面命令';
 }
 return msg;
}
const labels={artifact:'原始文件',chars:'字符数',request_kind:'请求用途',attempt:'请求尝试',latency_ms:'模型请求耗时（毫秒）',duration_ms:'操作耗时（毫秒）',in_tok:'输入 token',out_tok:'输出 token',status:'结果',state:'阶段状态',adb_detail:'ADB 诊断',command:'ADB 命令',dump_ms:'控件树耗时（毫秒）',dump_exit:'控件树退出码',dump_stdout:'控件树输出',dump_stderr:'控件树错误',remote_file:'设备文件检查',stage:'失败阶段',adb_exit:'ADB 退出码',shot_ms:'截图耗时（毫秒）',sig:'画面指纹',glms:'模型调用次数',calls:'模型请求次数',decisions:'后续观察次数',model_ms:'首次模型请求耗时（毫秒）',model_tokens:'首次模型用量',finish_after_launch:'启动应用后结束任务',candidate_tokens:'控件候选预计 token',omitted_actionable:'被裁剪的可操作控件',omitted_passive:'被裁剪的静态文本',target_lost:'目标候选被裁剪',node_count:'控件数',candidate_count:'候选数',no_progress:'连续无进展次数',wait_ms:'等待时间（毫秒）',result:'工具返回',package:'应用包名',app:'应用',pending_ms:'已等待（毫秒）',retry_wait_ms:'重试等待（毫秒）',error_type:'错误类型',elapsed:'已耗时（秒）',tokens:'本次用量',usage_total:'累计用量',routes:'路径次数',usage:'模型用量',retries:'重试次数'};
function technical(a){return Object.entries(a).filter(([k,v])=>!['action','summary','explain','task','call_id','pid','process_key','started_at','schema_version','run_log','usage_scope','usage_complete'].includes(k)&&v!==''&&v!=null).map(([k,v])=>'<b>'+esc(labels[k]||k)+'：</b>'+esc(k==='request_kind'?(kindName(v)||v):(typeof v==='object'?JSON.stringify(v):v))).join(' · ');}
function timing(s){
 const parts=[s.wall_time||`任务开始后 ${s.at_time??'?'} 秒`];
 if(s.inference_time!=null)parts.push(`模型请求 ${s.inference_time}s`);
 if(s.execution_time!=null)parts.push(`执行 ${s.execution_time}s`);
 else if(s.duration_time!=null)parts.push(`阶段耗时 ${s.duration_time}s`);
 if(s.inference_time==null&&s.execution_time==null&&s.gap_time>0)parts.push(`距上条日志 ${s.gap_time}s`);
 if(s.execution_time==null&&s.action?.action==='act'&&s.action?.state!=='start')parts.push('执行耗时未采集');
 if(s.action?.action==='model'&&s.action?.state==='end'&&s.action?.usage_known===false)parts.push('token 未采集');
 const t=s.tokens||{};if(num(t.input)+num(t.output))parts.push(`token ${num(t.input)+num(t.output)}（入 ${num(t.input)} / 出 ${num(t.output)}）`);
 return parts.join(' · ');
}
function diagnostic(s){const a=s.action||{};return ['glm_request','glm_response'].includes(a.action)||(a.action==='xml'&&a.status==='ok')||(a.action==='model'&&a.state==='progress')||(a.action==='guard'&&a.summary==='通过')||(a.action==='route'&&a.summary==='评估')||(a.action==='setup'&&String(a.summary).startsWith('debug 目录'));}
function finishHTML(a){if(a.action!=='finish')return '';const u=a.usage||{};return '<div class="meta">'+esc(`模型调用 ${u.calls??'未采集'} 次 · 重试 ${u.retries??'未采集'} 次 · 请求累计 ${u.latency_ms==null?'未采集':round(u.latency_ms/1000)+'s'}`)+'</div>'+(Object.keys(a.routes||{}).length?'<div class="meta">路径统计：'+esc(Object.entries(a.routes).map(([k,v])=>`${k} ${v}`).join(' · '))+'</div>':'');}
function card(s,taskDir,mini=false){
 const a=s.action||{},phase=String(a.action||'log').replace(/[^a-z_]/gi,''),tech=technical(a);
 const xml=a.action==='xml'&&a.artifact?'<a href="/api/artifact?task='+encodeURIComponent(taskDir)+'&amp;file='+encodeURIComponent(a.artifact)+'" target="_blank" rel="noopener">查看原始 XML</a>':'';
 return `<div class="${mini?'live-item':'card p-'+phase}" data-event="${s.idx}"><div class="${mini?'mini-body':'body'}"><div class="${mini?'mini-head':'row1'}"><span class="${mini?'idx-no':'step-no'}">${s.step===0?'准备':'第 '+s.step+' 步'} · ${s.substep}</span><span class="atype a-${phase}" title="${esc(a.action)}">${esc(phases[a.action]||a.action||'日志')}</span></div><div class="${mini?'mini-msg':'meta'}"><b>${esc(summary(a))}</b></div>${finishHTML(a)}${xml}${tech?'<details class="meta"><summary>技术详情</summary>'+tech+'</details>':''}<div class="${mini?'mini-meta':'timing'}">${esc(timing(s))}</div></div></div>`;
}
function stats(d){const s=d.stats,u=d.usage||{};return [
 ['任务',d.title||d.task],['状态',(statusNames[d.status]||d.status)+(d.reason?' · '+(reasons[d.reason]||d.reason):'')],['步数',s.total_steps],
 [d.complete||d.status==='running'?'任务总耗时':'已记录耗时',s.total_time+'s'],['平均每步（含启动）',s.avg_step==null?'—':s.avg_step+'s'],
 [s.usage_complete?'总 Token':'已记录 Token',s.usage_known?s.total_tokens:'未采集']
 ].map(([k,v])=>{
  const style=(k==='任务'||k==='状态'?'font-size:14px;':'')+'white-space:normal;overflow-wrap:anywhere';
  const content=k==='状态'?'<span class="badge '+esc(d.status)+'">'+esc(statusNames[d.status]||d.status)+'</span>'+(d.reason?'<div class="meta">'+esc(reasons[d.reason]||d.reason)+'</div>':''):esc(v);
  return '<div class="stat"><div class="k">'+esc(k)+'</div><div class="v" style="'+style+'">'+content+'</div></div>';
 }).join('');}
async function fetchJSON(url,options={}){
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
 try{const r=await fetch(url,{...options,signal:controller.signal,cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json();if(d.error||d.ok===false)throw new Error(d.error||'请求失败');return d;}finally{clearTimeout(timeout);}
}
const api={project,token,summary,technical,timing,diagnostic,card,stats,fetchJSON,statusNames,phases,esc};
root.LogView=api;if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);

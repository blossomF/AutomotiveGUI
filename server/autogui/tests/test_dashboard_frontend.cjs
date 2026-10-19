/* Exercise the actual embedded UI scripts with a deterministic DOM and clock.
   No browser/network packages are needed. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../../..');
const result=spawnSync('python',['-c',`import importlib.util,json; s=importlib.util.spec_from_file_location('frontend_test',${JSON.stringify(path.join(root,'dist/dashboard_server.py'))});m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps({k:getattr(m,k) for k in ['RUN_JS','LOGS_JS']}))`],{encoding:'utf8'});
if(result.status!==0)throw new Error(result.stderr);
const scripts=JSON.parse(result.stdout);
for(const file of ['dist/dashboard_server.py','dist/dashboard.html']){
 const source=fs.readFileSync(path.join(root,file),'utf8');
 assert.doesNotMatch(source,/id="(?:run|logs|offline)(?:Debug|Older)"/);
}
const view=require(path.join(root,'dist/log_view.js'));
class Element{
 constructor(){this.children=[];this.dataset={};this.classList={toggle(){},add(){},remove(){}};this.scrollHeight=100;this.scrollTop=100;this.clientHeight=100;this.value='';this.textContent='';this.hidden=false;}
 set innerHTML(html){this.html=html;this.children=[];for(const match of html.matchAll(/data-event="(\d+)"/g)){const e=new Element();e.dataset.event=match[1];this.appendChild(e);}}
 get innerHTML(){return this.html||'';}
 get firstElementChild(){return this.children[0]||null;}
 get childElementCount(){return this.children.length;}
 appendChild(e){if(e.fragment){for(const c of [...e.children])this.appendChild(c);return e;}e.parent=this;this.children.push(e);return e;}
 replaceChildren(){this.children=[];this.html='';}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(e=>e!==this);}
 addEventListener(){}
}
function environment(script,fetchJSON){
 const elements=new Map();const $=name=>{if(!elements.has(name))elements.set(name,new Element());return elements.get(name);};
 let timerId=0;const timers=new Map();
 const c={console,window:{},localStorage:{getItem:()=>null,setItem(){},removeItem(){}},location:{search:''},URLSearchParams,
  $,LogView:{...view,fetchJSON},esc:view.esc,setMascot(){},setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),
  setInterval:()=>1,clearInterval(){},requestAnimationFrame:fn=>fn(),document:{createElement:()=>new Element(),createDocumentFragment:()=>Object.assign(new Element(),{fragment:true})}};
 vm.createContext(c);vm.runInContext(script,c);return {c,$,timers,run:code=>vm.runInContext(code,c)};
}
const row={idx:0,step:1,substep:1,at_time:1,tokens:{input:12,output:4},action:{action:'decide',summary:'CLICK — 搜索'}};
function data(extra={}){return {ok:true,status:'running',task:'test',reason:'',stats:{total_steps:1,total_time:90,total_tokens:16},cursor:0,updates:[row],last:row,has_more:false,...extra};}
(async()=>{
 let requests=0;const run=environment(scripts.RUN_JS,async()=>{requests++;return data();});
 run.run("track('test');pollEnabled=true");
 for(let i=0;i<20;i++)await run.run('tick()');
 assert.equal(requests,20);assert.equal(run.run('pollEnabled'),true);assert.equal(run.run('liveSteps.length'),1);
 assert.match(run.$('#live').textContent,/继续等待并刷新/);assert.equal(run.$('#liveList').childElementCount,1);
 // Transient failure retains tracking and retries.
 run.c.LogView.fetchJSON=async()=>{throw new Error('offline');};await run.run('tick()');
 assert.match(run.$('#live').textContent,/正在重试/);assert.equal(run.run('pollEnabled'),true);
 // Completion includes the final summary, and stops scheduling.
 const finish={...row,idx:1,action:{action:'finish',summary:'complete',usage:{calls:1,retries:0,latency_ms:500}}};
 run.c.LogView.fetchJSON=async()=>data({status:'done',cursor:1,updates:[finish],last:finish});
 await run.run('tick()');assert.equal(run.run('pollEnabled'),false);assert.equal(run.run('liveSteps.length'),2);
 // A request in progress prevents a second poll; switching tasks ignores old replies.
 let release;run.c.LogView.fetchJSON=()=>new Promise(r=>release=r);
 run.run("track('old');pollEnabled=true");const pending=run.run('tick()');await run.run('tick()');
 run.run("track('new')");release(data());await pending;assert.equal(run.run('liveSteps.length'),0);
 // Logs append incrementally without duplicating old rows; manual pause survives refresh.
 let logRequests=0;const logs=environment(scripts.LOGS_JS,async url=>{
  if(url==='/api/tasks')return [];
  logRequests++;return data({updates:logRequests===1?[row]:[],cursor:0});
 });
 await logs.run("loadTask('test')");const original=logs.$('#timeline').firstElementChild;
 await logs.run("loadTask('test')");assert.equal(logs.$('#timeline').firstElementChild,original);
 logs.run('autoRefresh=false');await logs.run("loadTask('test')");assert.equal(logs.$('#autoBtn').dataset.on,'0');
 logs.run('autoRefresh=true');logs.c.LogView.fetchJSON=async()=>data({status:'done',updates:[]});
 await logs.run("loadTask('test')");assert.equal(logs.$('#autoBtn').dataset.on,'0');
 logs.$('#autoBtn').onclick();assert.equal(logs.run('autoRefresh'),true);
 // All diagnostics and earlier records stay visible, including across API pages.
 const all=Array.from({length:260},(_,idx)=>({...row,idx,action:{action:idx%2?'glm_request':'xml',status:'ok',summary:'debug'}}));
 const full=environment(scripts.RUN_JS,async()=>data());full.run("track('test')");
 full.c.batch=data({updates:all.slice(0,200),cursor:199});full.run('renderLive(batch)');
 full.c.batch=data({updates:all.slice(200),cursor:259});full.run('renderLive(batch)');
 assert.equal(full.$('#liveList').childElementCount,260);assert.equal(full.run('liveSteps[0].idx'),0);
 const fullLogs=environment(scripts.LOGS_JS,async url=>url==='/api/tasks'?[]:data({updates:all,cursor:259}));
 await fullLogs.run("loadTask('test')");assert.equal(fullLogs.$('#timeline').childElementCount,260);
 // Four artifact kinds are clickable and escaped; no file:// links are generated.
 for(const [phase,file,label] of [['xml','step01_0001_xml.xml','原始 XML'],['glm_request','step01_0002_glm_request.json','LLM Request'],['glm_response','step01_0003_glm_response.json','LLM Response']]){
  const html=view.card({...row,action:{action:phase,artifact:file}},'中文任务');
  assert.ok(html.includes(label));assert.ok(html.includes('file='+file));assert.ok(html.includes(encodeURIComponent('中文任务')));
  assert.ok(html.includes('target="_blank"'));assert.doesNotMatch(html,/file:\/\//);
 }
 assert.match(view.card({...row,action:{action:'decide',candidates_artifact:'step01_candidates.txt'}},'test'),/裁剪结果.*step01_candidates.txt/);
 assert.doesNotMatch(view.card({...row,action:{action:'glm_request',artifact:'../config.json'}},'test'),/<a /);
 // Filtering keeps model/XML errors visible, and never fabricates missing durations.
 assert.equal(view.diagnostic({action:{action:'xml',status:'error'}}),false);
 assert.equal(view.diagnostic({action:{action:'xml',status:'ok'}}),true);
 assert.doesNotMatch(view.timing(row),/推理 0|执行 0|cost:/);
 console.log('PASS frontend: polling, incremental DOM, pause, all diagnostics/history, four artifact links');
})().catch(error=>{console.error(error);process.exit(1);});

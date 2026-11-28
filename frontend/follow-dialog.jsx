import {api,post} from './api.js';
import {Modal,Notice} from './components.jsx';
import {RulesEditor,ReleaseInfo,initialRules,refreshFollows} from './tracking.jsx';

const providers={mikan:'蜜柑',dmhy:'动漫花园',nyaa:'Nyaa',acgrip:'ACG.RIP'};
const workId=work=>String(work?.canonical_id||work?.id||work?.mikan_id||'');
const workTitle=work=>work?.homeTitle||work?.title||work?.series_title||'';

export function FollowModal({work={},subscription,initialGroups=[],initialRules:inherited={},sourceGroupId='0',close,onSaved}){
 const {useState,useEffect,useRef}=window.Mikan115Runtime.React;
 const initialGroup=String(subscription?.subgroup_id||sourceGroupId||'0');
 const [selected,setSelected]=useState(subscription?.work||work),[query,setQuery]=useState(''),[results,setResults]=useState([]),[seasons,setSeasons]=useState(null);
 const [provider,setProvider]=useState(subscription&&!['bangumi','personal'].includes(subscription.source_kind)?subscription.source_kind:'mikan');
 const [url,setUrl]=useState(''),[useUrl,setUseUrl]=useState(false),[options,setOptions]=useState(null),[allGroups,setAllGroups]=useState([]);
 const [rule,setRule]=useState(()=>({...initialRules,...(subscription?.rules||inherited),...(!subscription&&initialGroups.length?{group_ids:initialGroups}:{}),...(initialGroup!=='0'?{group_ids:[initialGroup]}:{})}));
 const [preview,setPreview]=useState(null),[history,setHistory]=useState(null),[includeExisting,setIncludeExisting]=useState(false);
 const [busy,setBusy]=useState(''),[error,setError]=useState(''),[login,setLogin]=useState(false),[tmdb,setTmdb]=useState(false),sequence=useRef(0);
 const ident=workId(selected),fixedGroup=String(options?.source_subgroup_id||initialGroup||'0');
 const data=()=>({work_id:ident||undefined,provider,...(initialGroup!=='0'?{subgroup_id:initialGroup}:{}),...(useUrl&&url?{rss_url:url}:{}),rules:rule,include_existing:includeExisting});
 function fail(e){setError(e.message);setLogin(e.status===401);}
 useEffect(()=>{const c=new AbortController();api('/subtitle-groups',{signal:c.signal}).then(x=>setAllGroups(x.groups)).catch(()=>{});return()=>c.abort();},[]);
 useEffect(()=>{
  setOptions(null);setPreview(null);if(!subscription&&!ident&&!(useUrl&&url))return;
  if(!subscription&&useUrl&&!url)return;
  const c=new AbortController();let active=true;setBusy('options');setError('');
  const timer=setTimeout(async()=>{try{
   const result=subscription?await api(`/subscriptions/${subscription.id}/options`,{signal:c.signal}):await api('/subscriptions/options',{method:'POST',body:JSON.stringify(data()),signal:c.signal});
   if(active){setOptions(result);if(!ident&&result.work_id)setSelected(result.work);
    const season=result.work?.tmdb_season??null;setRule(r=>({...r,season,group_ids:result.source_subgroup_id&&result.source_subgroup_id!=='0'?[result.source_subgroup_id]:r.group_ids}));}
  }catch(e){if(active&&e.name!=='AbortError')fail(e);}finally{if(active)setBusy('');}},200);
  return()=>{active=false;clearTimeout(timer);c.abort();};
 },[ident,provider,useUrl,url,subscription?.id]);
 useEffect(()=>{
  if(!options||options.source_required||options.work_required)return;
  const c=new AbortController(),current=++sequence.current;setPreview(null);setBusy('preview');
  const timer=setTimeout(async()=>{try{const result=await api(subscription?`/subscriptions/${subscription.id}/preview`:'/subscriptions/preview',{method:'POST',body:JSON.stringify(data()),signal:c.signal});if(current===sequence.current)setPreview(result);}
   catch(e){if(current===sequence.current&&e.name!=='AbortError')fail(e);}finally{if(current===sequence.current)setBusy('');}},250);
  return()=>{clearTimeout(timer);c.abort();sequence.current++;};
 },[options,rule,includeExisting]);
 async function search(e,live=false){e?.preventDefault();setBusy('search');setError('');setSeasons(null);
  try{const result=await api('/subscriptions/search?'+new URLSearchParams({q:query,tmdb:live?'1':'0'}));setResults(result.results);setTmdb(result.tmdb_configured);}catch(e){fail(e);}finally{setBusy('');}}
 async function choose(item,season){setError('');if(!item.needs_season){setSelected(item);setResults([]);return;}
  setBusy('choose');try{const result=await post('/subscriptions/tmdb',{tmdb_id:item.tmdb_id,...(season!==undefined?{season}:{})});if(result.work){setSelected(result.work);setResults([]);setSeasons(null);}else setSeasons({...result,item});}catch(e){fail(e);}finally{setBusy('');}}
 async function save(){if(!preview)return;setBusy('save');setError('');try{
  if(subscription)await post('/subscriptions/'+subscription.id,{rules:rule});
  else {const created=await post('/subscriptions',data());if(created.existing)await post('/subscriptions/'+created.id,{rules:rule});}
  const result=await api('/subscriptions');refreshFollows(result.subscriptions);onSaved?.();close();
 }catch(e){fail(e);}finally{setBusy('');}}
 const matched=preview?.items.filter(item=>item.admitted).length||0;
 return <Modal title={subscription?'追新规则':'添加追新'} close={close} footer={<button className="primary-button" disabled={!!busy||login||!preview} onClick={save}>{busy==='save'?'保存中…':subscription?'保存':'开始追新'}</button>}>
  {error&&<Notice error={error}/>} {login&&<button className="primary-button" onClick={()=>{close();window.location.hash='/login';}}>登录</button>}
  {ident?<div className="m115-follow-work"><strong>{workTitle(selected)||subscription?.title}</strong><span>{selected.tmdb_id?'TMDB '+selected.tmdb_id+(selected.tmdb_season>1?' · 第 '+selected.tmdb_season+' 季':''):'待关联 TMDB'}</span>{!subscription&&<button className="secondary-button" onClick={()=>{setSelected({});setOptions(null);setPreview(null);}}>更换动漫</button>}</div>:!subscription&&<>
   <form className="m115-search" onSubmit={search}><input type="search" aria-label="搜索动漫" placeholder="搜索动漫" value={query} onChange={e=>setQuery(e.target.value)} required/><button className="secondary-button" disabled={!!busy}>搜索</button>{tmdb&&<button type="button" className="secondary-button" disabled={!!busy||!query} onClick={()=>search(null,true)}>搜索 TMDB</button>}</form>
   <div className="m115-source-list">{results.map((item,index)=><button className="m115-source" key={item.id||'tmdb-'+item.tmdb_id+'-'+index} onClick={()=>choose(item)}><strong>{workTitle(item)}</strong><small>{item.year||''}{item.tmdb_id?' · TMDB '+item.tmdb_id+(item.tmdb_season>1?' · 第 '+item.tmdb_season+' 季':''):' · 蜜柑'}</small></button>)}</div>
   {seasons&&<div className="m115-source-list" aria-label="选择季度">{seasons.seasons.map(s=><button className="m115-source" key={s.season_number} onClick={()=>choose(seasons.item,s.season_number)}>{s.name} · {s.air_date||''} · {s.episode_count} 集</button>)}</div>}
  </>}
  {!subscription&&<><div className="m115-form"><label>来源<select aria-label="来源" value={provider} onChange={e=>{setProvider(e.target.value);setUseUrl(false);setRule(r=>({...r,group_ids:[]}));}}>{Object.entries(providers).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label></div><details className="m115-feed-link" onToggle={e=>setUseUrl(e.currentTarget.open)}><summary>使用 RSS 链接</summary><label>RSS 或搜索链接<input type="url" value={url} onChange={e=>setUrl(e.target.value)} autoComplete="off" spellCheck="false" placeholder="https://…"/></label></details></>}
  {options?.work_required&&<Notice>请选择对应动漫</Notice>}
  {options&&!options.source_required&&!options.work_required&&<>
   <RulesEditor value={rule} onChange={setRule} groups={options.groups||[]} allGroups={provider==='mikan'?allGroups:[]} fixedGroupId={fixedGroup!=='0'?fixedGroup:null}/>
   {!subscription&&<label className="m115-checkbox"><input type="checkbox" checked={includeExisting} onChange={e=>setIncludeExisting(e.target.checked)}/>同时收集现有剧集</label>}
  </>}
  {!!busy&&<Notice>{busy==='preview'?'匹配中…':'加载中…'}</Notice>}
  {preview&&<section className="m115-match-preview"><h3>匹配 {matched} 项 · 跳过 {preview.items.length-matched} 项</h3>{preview.items.slice(0,30).map((item,index)=><article key={item.key+index}><strong>{item.title}</strong><ReleaseInfo release={item.release}/><small className={item.admitted?'m115-accepted':'muted'}>{item.reason||(item.previous==='BASELINE'?'创建前资源':item.admitted?'符合规则':'已跳过')}</small></article>)}{!preview.items.length&&<p className="muted">等待更新</p>}</section>}
  {subscription&&<details onToggle={e=>{if(e.currentTarget.open&&!history)api(`/subscriptions/${subscription.id}/history`).then(setHistory).catch(fail);}}><summary>处理记录</summary>{history?.items.map((item,index)=><article className="m115-follow-history" key={index}><strong>{item.title}</strong><ReleaseInfo release={item.release}/><small>{item.reason||item.status}</small></article>)}</details>}
 </Modal>;
}

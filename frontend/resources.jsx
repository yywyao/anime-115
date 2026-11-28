import {api,post} from './api.js';
import {Modal,Notice,displayTitle} from './components.jsx';
import {FollowModal,ReleaseInfo,useFollowing} from './tracking.jsx';
import {releaseId,resolutions,subtitleVersions,toggleGroupSelection} from './release-selection.js';
import {selectionBody,effectiveSelection,episodeRanges} from './collection-plan.js';

const emptyFilters=()=>({group_ids:[],resolutions:[],versions:[]});
function FilterChoices({label,values,choices,onChange}){
 return <fieldset className="m115-resource-filter"><legend>{label}</legend>
  <button type="button" aria-pressed={!values.length} onClick={()=>onChange([])}>不限</button>
  {Object.entries(choices).map(([id,name])=><label key={id}><input type="checkbox" checked={values.includes(id)} onChange={e=>onChange(e.target.checked?[...values,id]:values.filter(v=>v!==id))}/>{name}</label>)}
 </fieldset>;
}
export function ResourceModal({work,close}){
 const {useState,useEffect,useRef}=window.Mikan115Runtime.React;
 const [data,setData]=useState(null),[filters,setFilters]=useState(emptyFilters),[selected,setSelected]=useState(null);
 const [expanded,setExpanded]=useState(new Set()),[follow,setFollow]=useState(null),[sources,setSources]=useState([]);
 const [term,setTerm]=useState(work.homeTitle||work.title||''),[action,setAction]=useState('missing'),[destination,setDestination]=useState('cloud');
 const [plan,setPlan]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState('load'),[planning,setPlanning]=useState(false);
 const [message,setMessage]=useState(''),[login,setLogin]=useState(false),[refresh,setRefresh]=useState(0);
 const lastRefresh=useRef(0),request=useRef(0),invoker=useRef(document.activeElement),subscriptions=useFollowing();
 const ident=String(work.canonical_id||work.id||work.mikan_id||work.bangumiId||'');
 const bid=String(data?.bangumi_id||work.mikan_id||work.bangumiId||'');
 const groups=data?.groups||[],shown=plan?.groups||[],checked=effectiveSelection(selected,plan);
 const loading=!!busy||planning;
 const currentSubscription=group=>subscriptions.find(s=>(String(s.bangumi_id)===bid||!!plan?.scope_key&&s.scope_key===plan.scope_key)&&String(s.subgroup_id||'0')===String(group||'0'));
 function failed(e){setError(e.message);if(e.status===401)setLogin(true);}
 function body(ids){return {mikan_id:bid,action,destination,rules:filters,...(ids?{release_ids:ids}:selectionBody(selected))};}
 useEffect(()=>()=>queueMicrotask(()=>{if(invoker.current?.isConnected)invoker.current.focus();}),[]);
 async function load(source){const sequence=++request.current;setBusy('load');setError('');setPlan(null);setSelected(null);
  try{const result=await api('/resources?'+new URLSearchParams({work_id:ident,...(source?{bangumi_id:source}:{})}));if(sequence===request.current)setData(result);}
  catch(e){if(sequence===request.current)failed(e);}finally{if(sequence===request.current)setBusy('');}}
 useEffect(()=>{load();return()=>{request.current++;};},[ident]);
 useEffect(()=>{
  if(!bid||!data||data.source_required||login)return;
  const controller=new AbortController();let active=true;setPlanning(true);setError('');
  const timer=setTimeout(async()=>{try{const result=await api('/library/actions',{method:'POST',body:JSON.stringify({...body(),refresh:refresh>lastRefresh.current}),signal:controller.signal});if(active){setPlan(result);lastRefresh.current=refresh;}}
   catch(e){if(active&&e.name!=='AbortError')failed(e);}finally{if(active)setPlanning(false);}},180);
  return()=>{active=false;clearTimeout(timer);controller.abort();};
 },[bid,data,action,destination,filters,selected,refresh,login]);
 function changeFilters(key,value){setFilters({...filters,[key]:value});setSelected(null);setPlan(null);setMessage('');}
 function toggle(resource){const next=new Set(checked),id=releaseId(resource);next.has(id)?next.delete(id):next.add(id);setSelected(next);setPlan(null);}
 function selectGroup(group){const ids=group.resources.map(releaseId);setSelected(selected===null?new Set(ids.every(id=>checked.has(id))?[]:ids):toggleGroupSelection(selected,group.resources));setPlan(null);}
 async function search(e){e.preventDefault();setBusy('search');setError('');try{const result=await api('/search?q='+encodeURIComponent(term));setSources((result.results||[]).filter(w=>/^\d+$/.test(String(w.mikan_id||w.bangumiId||''))));}catch(e){failed(e);}finally{setBusy('');}}
 async function submit(ids){if(!plan||loading)return;setBusy('submit');setError('');setMessage('');
  try{let requestBody=body(ids);const reviewed=ids?await post('/library/actions',requestBody):plan;
   const result=await post('/library/actions',{...requestBody,digest:reviewed.digest});setSelected(null);setPlan(null);setRefresh(v=>v+1);
   setMessage((destination==='cloud'?'已提交转存 ':'已加入 VPS 下载队列 ')+(result.tasks?.length||0)+' 项');
  }catch(e){failed(e);setPlan(null);setRefresh(v=>v+1);}finally{setBusy('');}}
 function startFollowing(group){const ids=group?[String(group.subgroupId)]:filters.group_ids,sg=ids.length===1?ids[0]:'0';setFollow({ids,sg,rules:{...filters,group_ids:ids,download_to_vps:destination==='vps',wait_minutes:0},subscription:currentSubscription(sg)});}
 function go(path){close();window.location.hash=path;window.scrollTo({top:0,left:0,behavior:'instant'});}
 if(follow)return <FollowModal work={{...work,mikan_id:bid}} subscription={follow.subscription} initialGroups={follow.ids} initialRules={follow.rules} sourceGroupId={follow.sg} close={()=>setFollow(null)} onSaved={()=>setFollow(null)}/>;
 const button=destination==='cloud'?'转存至 115':'下载到 VPS';
 return <Modal title={work.homeTitle||work.title||work.series_title||data?.title||'选择资源'} close={close} footer={<>
  <button className="secondary-button" disabled={loading||!bid} onClick={()=>startFollowing()}>{currentSubscription(filters.group_ids.length===1?filters.group_ids[0]:'0')?'管理追新':'追新'}</button>
  <button className="primary-button" disabled={loading||!plan?.items.length||!plan.inventory_verified} onClick={()=>submit()}>{busy==='submit'?'提交中…':button+'（'+(plan?.selected_episodes.length||0)+' 集）'}</button>
  <button className="secondary-button" onClick={()=>go('/requests')}>任务</button>
 </>}>
  {error&&<Notice error={error}/>} {message&&<Notice>{message}</Notice>}
  {login&&<button className="primary-button" onClick={()=>go('/login')}>登录</button>}
  {data?.source_required&&<><form className="m115-search" onSubmit={search}><input aria-label="搜索资源" value={term} onChange={e=>setTerm(e.target.value)}/><button className="primary-button" disabled={loading}>搜索</button></form><div className="m115-source-list">{sources.map(s=><button className="m115-source" key={s.id} onClick={()=>load(String(s.mikan_id||s.bangumiId))}>{displayTitle(s)}</button>)}</div></>}
  {!!groups.length&&<>
   <section className="m115-resource-filters" aria-label="资源筛选">
    <FilterChoices label="分辨率" values={filters.resolutions} choices={resolutions} onChange={v=>changeFilters('resolutions',v)}/>
    <FilterChoices label="字幕" values={filters.versions} choices={subtitleVersions} onChange={v=>changeFilters('versions',v)}/>
    <details className="m115-resource-group-filter"><summary>字幕组{filters.group_ids.length?' · 已选 '+filters.group_ids.length+' 组':' · 不限'}</summary><button className="secondary-button" onClick={()=>changeFilters('group_ids',[])}>全部字幕组</button><div className="m115-group-options">{groups.map(g=><label key={g.subgroupId}><input type="checkbox" checked={filters.group_ids.includes(String(g.subgroupId))} onChange={e=>changeFilters('group_ids',e.target.checked?[...filters.group_ids,String(g.subgroupId)]:filters.group_ids.filter(v=>v!==String(g.subgroupId)))}/><span>{g.subgroupName}</span></label>)}</div></details>
   </section>
   <section className="m115-library-actions" aria-label="收集方式">
    <div className="m115-destination-tabs" role="group" aria-label="保存位置">{[['cloud','转存至 115'],['vps','下载到 VPS']].map(([key,label])=><button type="button" key={key} aria-pressed={destination===key} disabled={!!busy} onClick={()=>{setDestination(key);setSelected(null);setPlan(null);setMessage('');}}>{label}</button>)}</div>
    <div className="m115-heading"><h3>{destination==='cloud'?'115 网盘':'VPS 文件'}</h3><select aria-label="入库动作" value={action} onChange={e=>{setAction(e.target.value);setSelected(null);setPlan(null);setMessage('');}}>
     <option value="missing" title="只收集已播出且缺少的剧集">补缺集</option><option value="complete" title="收集当前可用剧集，跳过已有剧集">收集全部</option><option value="upgrade" title="为已有剧集收集所选版本，保留原文件">洗版</option></select></div>
    {plan&&<div className="m115-inventory-summary"><span title={episodeRanges(plan.owned_episodes)}>已有 {plan.owned_episodes.length} 集</span><span title={episodeRanges(plan.pending_episodes)}>处理中 {plan.pending_episodes.length} 集</span><span title={episodeRanges(plan.missing_episodes)}>缺少 {plan.missing_episodes.length} 集</span><button className="m115-text-button" disabled={loading} onClick={()=>{setPlan(null);setRefresh(v=>v+1);}}>刷新库存</button></div>}
    {plan?.missing_episodes.length>0&&<p className="m115-episode-list">缺集 {episodeRanges(plan.missing_episodes)}</p>}
    {plan?.inventory_error&&<Notice error={plan.inventory_error}/>}
    {plan?.unknown_resources>0&&<span className="muted">集号未确认 {plan.unknown_resources} 项</span>}
   </section>
  </>}
  {shown.map(group=>{const ids=group.resources.map(releaseId),all=ids.every(id=>checked.has(id)),open=expanded.has(String(group.subgroupId));return <section className="m115-resource-group" key={group.subgroupId} aria-label={group.subgroupName}>
   <div className="m115-resource-group-heading"><h3>{group.subgroupName}<small>{group.episode_count} 集 · {group.resources.length} 项</small></h3><div className="m115-resource-group-actions"><button className="secondary-button" aria-pressed={all} disabled={loading} onClick={()=>selectGroup(group)}>{all?'取消全选':'全选'}</button><button className="secondary-button" disabled={loading} onClick={()=>startFollowing(group)}>{currentSubscription(group.subgroupId)?'管理追新':'追新'}</button></div></div>
   {(open?group.resources:group.resources.slice(0,3)).map(resource=>{const id=releaseId(resource);return <article className="m115-resource" key={id}><label className="m115-resource-select"><input type="checkbox" aria-label={'选择 '+resource.title} checked={checked.has(id)} disabled={loading} onChange={()=>toggle(resource)}/></label><div><strong>{resource.title}</strong><ReleaseInfo release={resource.release}/><small>{[resource.size,resource.pubDate].filter(Boolean).join(' · ')}</small></div><div className="m115-resource-destinations"><button className="secondary-button" disabled={loading||!plan.inventory_verified} onClick={()=>submit([id])}>{button}</button></div></article>;})}
   {group.resources.length>3&&<button className="secondary-button m115-resource-expand" aria-expanded={open} onClick={()=>setExpanded(previous=>{const next=new Set(previous);open?next.delete(String(group.subgroupId)):next.add(String(group.subgroupId));return next;})}>{open?'收起资源':'查看全部 '+group.resources.length+' 项'}</button>}
  </section>;})}
  {(busy==='load'||planning)&&<Notice>核对中…</Notice>}
  {plan&&!shown.length&&!planning&&<p className="muted">{action==='upgrade'?'暂无可洗版资源':plan.missing_episodes.length?'暂无匹配资源':'暂无待收集剧集'}</p>}
 </Modal>;
}

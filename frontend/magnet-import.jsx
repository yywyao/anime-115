import {api,post} from './api.js';
import {Notice} from './components.jsx';
import {ReleaseInfo} from './tracking.jsx';

export function MagnetImportForm({onCreated}){
 const {useState,useEffect}=window.Mikan115Runtime.React;
 const [text,setText]=useState(''),[parsed,setParsed]=useState(false),[plan,setPlan]=useState(null),[selected,setSelected]=useState(null);
 const [bindings,setBindings]=useState({}),[destination,setDestination]=useState('cloud'),[revision,setRevision]=useState(0);
 const [queries,setQueries]=useState({}),[results,setResults]=useState({}),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
 const body=()=>({text,bindings,destination,...(selected===null?{}:{selected:[...selected]})});
 useEffect(()=>{
  if(!parsed)return;const controller=new AbortController();let active=true;setBusy(true);setError('');
  const timer=setTimeout(async()=>{try{const result=await api('/transfers/import',{method:'POST',body:JSON.stringify(body()),signal:controller.signal});if(active)setPlan(result);}
   catch(e){if(active&&e.name!=='AbortError'){setError(e.message);setPlan(null);}}finally{if(active)setBusy(false);}},180);
  return()=>{active=false;clearTimeout(timer);controller.abort();};
 },[parsed,bindings,destination,selected,revision]);
 const groups=Object.values((plan?.items||[]).reduce((groups,item)=>{const key=item.group_key;(groups[key]??={key,title:item.series_label,name:item.release.group||'未标注字幕组',items:[]}).items.push(item);return groups;},{}));
 const checked=new Set((plan?.items||[]).filter(item=>item.selected).map(item=>item.infohash));
 function toggle(items){const next=new Set(selected===null?checked:selected),all=items.filter(item=>!item.reason).every(item=>next.has(item.infohash));for(const item of items){if(item.reason)continue;all?next.delete(item.infohash):next.add(item.infohash);}setSelected(next);}
 async function search(group){setError('');try{const result=await api('/subscriptions/search?q='+encodeURIComponent(queries[group.key]??group.title));setResults(old=>({...old,[group.key]:result.results.filter(work=>work.id&&!work.needs_season)}));}catch(e){setError(e.message);}}
 async function submit(){if(!plan||busy)return;setBusy(true);setError('');try{const result=await post('/transfers/import',{...body(),digest:plan.digest});setMessage((destination==='cloud'?'已提交转存 ':'已加入 VPS 下载队列 ')+result.tasks.length+' 项'+(result.merged?' · 合并 '+result.merged+' 项':''));if(result.failed?.length){setError('有 '+result.failed.length+' 项未完成');setSelected(new Set(result.failed.map(item=>item.infohash)));setRevision(v=>v+1);}else{setParsed(false);setPlan(null);setText('');setBindings({});setSelected(null);}onCreated?.();}catch(e){setError(e.message);setRevision(v=>v+1);}finally{setBusy(false);}}
 return <div className="m115-form m115-manual">
  <label>磁力链接<textarea aria-label="磁力链接" rows="4" maxLength="60000" value={text} onChange={e=>{setText(e.target.value);setParsed(false);setPlan(null);setSelected(null);setBindings({});setMessage('');}} placeholder="粘贴一个或多个 magnet 链接"/></label>
  <div className="m115-actions"><button type="button" className="secondary-button" disabled={busy||!text.trim()} onClick={()=>{setParsed(true);setRevision(v=>v+1);}}>读取链接</button></div>
  {error&&<Notice error={error}/>} {message&&<Notice>{message}</Notice>}
  {plan&&<>
   <div className="m115-destination-tabs" role="group" aria-label="磁力保存位置">{[['cloud','转存至 115'],['vps','下载到 VPS']].map(([key,name])=><button type="button" key={key} aria-pressed={destination===key} disabled={busy} onClick={()=>setDestination(key)}>{name}</button>)}</div>
   <div className="m115-inventory-summary"><span>{plan.items.length} 个资源 · {groups.length} 组</span>{plan.duplicates>0&&<span>已合并重复链接 {plan.duplicates} 个</span>}</div>
   {groups.map(group=><section className="m115-magnet-group" key={group.key}>
    <div className="m115-resource-group-heading"><h3>{group.title}<small>{group.name} · {group.items.length} 项</small></h3><button className="secondary-button" disabled={busy} onClick={()=>toggle(group.items)}>{group.items.filter(item=>!item.reason).every(item=>checked.has(item.infohash))?'取消全选':'全选'}</button></div>
    <details className="m115-magnet-binding" onToggle={e=>{if(e.currentTarget.open&&!results[group.key])search(group);}}><summary>{bindings[group.key]?'已关联动漫':'关联动漫'}</summary>
     <div className="m115-search"><input aria-label={'搜索 '+group.title} value={queries[group.key]??group.title} onChange={e=>setQueries({...queries,[group.key]:e.target.value})}/><button className="secondary-button" onClick={()=>search(group)}>搜索</button></div>
     <select aria-label={'作品 '+group.key} value={bindings[group.key]||''} onChange={e=>{const next={...bindings};e.target.value?next[group.key]=e.target.value:delete next[group.key];setBindings(next);}}><option value="">未关联作品</option>{(results[group.key]||[]).map(work=><option key={work.id} value={work.id}>{work.homeTitle||work.title}{work.year?' · '+work.year:''}</option>)}</select>
    </details>
    {group.items.map(item=><label className="m115-magnet-row" key={item.infohash}><input type="checkbox" disabled={busy||!!item.reason} checked={checked.has(item.infohash)} onChange={()=>toggle([item])}/><span><strong>{item.title}</strong><ReleaseInfo release={item.release}/><small>{item.reason||(item.release.episodes.length?'第 '+item.release.episodes.join('、')+' 集':'集号未确认')}</small></span></label>)}
   </section>)}
   {plan.invalid.map(item=><Notice key={item.index} error={'第 '+item.index+' 个链接：'+item.reason}/>)}
   {!plan.connection_ready&&<Notice>请先在设置中连接 115</Notice>}
   <button className="primary-button" disabled={busy||!plan.selected_count||!plan.connection_ready} onClick={submit}>{busy?'处理中…':(destination==='cloud'?'转存至 115':'下载到 VPS')+'（'+plan.selected_count+'）'}</button>
  </>}
 </div>;
}

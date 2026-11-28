import {api,post} from './api.js';
import {Modal,Notice,displayTitle} from './components.jsx';
import {FollowModal,ReleaseInfo,useFollowing} from './tracking.jsx';
import {filterGroups,releaseId,resolutions,subtitleVersions,toggleGroupSelection} from './release-selection.js';

const emptyFilters=()=>({group_ids:[],resolutions:[],versions:[]});

function FilterChoices({label,values,choices,onChange}){
  return <fieldset className="m115-resource-filter">
    <legend>{label}</legend>
    <button type="button" aria-pressed={!values.length} onClick={()=>onChange([])}>不限</button>
    {Object.entries(choices).map(([id,name])=><label key={id}>
      <input type="checkbox" checked={values.includes(id)} onChange={event=>{
        onChange(event.target.checked?[...values,id]:values.filter(value=>value!==id));
      }}/>{name}
    </label>)}
  </fieldset>;
}

export function ResourceModal({work,close}){
  const {useState,useEffect,useRef}=window.Mikan115Runtime.React;
  const [data,setData]=useState(null);
  const [filters,setFilters]=useState(emptyFilters);
  const [selected,setSelected]=useState(new Set());
  const [expanded,setExpanded]=useState(new Set());
  const [follow,setFollow]=useState(null);
  const [sources,setSources]=useState([]);
  const [term,setTerm]=useState(work.title||work.cleanTitle||'');
  const [action,setAction]=useState('complete');
  const [plan,setPlan]=useState(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState('');
  const [message,setMessage]=useState('');
  const [login,setLogin]=useState(false);
  const request=useRef(0);
  const invoker=useRef(document.activeElement);
  const subscriptions=useFollowing();
  const ident=String(work.canonical_id||work.id||work.mikan_id||work.bangumiId||'');
  const bid=String(data?.bangumi_id||work.mikan_id||work.bangumiId||'');
  const groups=data?.groups||[];
  const shown=filterGroups(groups,filters);
  const matching=shown.flatMap(group=>group.resources);
  const visibleIds=new Set(matching.map(releaseId).filter(Boolean));
  const chosen=[...selected].filter(id=>visibleIds.has(id));
  const title=displayTitle(work);
  const currentSubscription=groupId=>subscriptions.find(item=>
    String(item.bangumi_id)===bid&&String(item.subgroup_id||'0')===String(groupId||'0')
  );

  useEffect(()=>()=>queueMicrotask(()=>{
    if(invoker.current?.isConnected)invoker.current.focus();
  }),[]);

  function failed(exception){
    setError(exception.message);
    if(exception.status===401)setLogin(true);
  }

  async function load(sourceId){
    const sequence=++request.current;
    setBusy('load');setError('');setSelected(new Set());setPlan(null);
    try{
      const query=new URLSearchParams({work_id:ident,...(sourceId?{bangumi_id:sourceId}:{})});
      const result=await api('/resources?'+query);
      if(sequence===request.current)setData(result);
    }catch(exception){
      if(sequence===request.current)failed(exception);
    }finally{
      if(sequence===request.current)setBusy('');
    }
  }

  useEffect(()=>{
    load();
    return()=>{request.current++;};
  },[ident]);

  function changeFilters(key,values){
    const next={...filters,[key]:values};
    const visible=new Set(filterGroups(groups,next).flatMap(group=>group.resources).map(releaseId).filter(Boolean));
    setFilters(next);
    setSelected(previous=>new Set([...previous].filter(id=>visible.has(id))));
    setPlan(null);setMessage('');
  }

  function toggle(resource){
    const id=releaseId(resource);
    if(!id||busy)return;
    setSelected(previous=>{
      const next=new Set(previous);
      next.has(id)?next.delete(id):next.add(id);
      return next;
    });
    setPlan(null);
  }

  function selectGroup(group){
    setSelected(previous=>toggleGroupSelection(previous,group.resources));
    setPlan(null);setMessage('');
  }

  async function search(event){
    event.preventDefault();
    setBusy('search');setError('');setMessage('');
    try{
      const result=await api('/search?q='+encodeURIComponent(term));
      const found=(result.results||[]).filter(item=>/^\d+$/.test(String(item.mikan_id||item.bangumiId||'')));
      setSources(found);
      if(!found.length)setMessage('未找到资源');
    }catch(exception){failed(exception);}
    finally{setBusy('');}
  }

  async function transfer(ids){
    if(!ids.length)return;
    setBusy('transfer');setError('');setMessage('');
    try{
      const result=await post('/transfers',{mikan_id:bid,release_ids:ids,rules:filters});
      setSelected(previous=>new Set([...previous].filter(id=>!ids.includes(id))));
      setPlan(null);
      setMessage('已提交 '+(result.tasks?.length||0)+' 项');
    }catch(exception){failed(exception);}
    finally{setBusy('');}
  }

  function actionBody(){
    return {action,mikan_id:bid,rules:filters,...(chosen.length?{release_ids:chosen}:{})};
  }

  async function previewLibrary(){
    setBusy('preview');setError('');setPlan(null);
    try{setPlan(await post('/library/actions',actionBody()));}
    catch(exception){failed(exception);}
    finally{setBusy('');}
  }

  async function confirmLibrary(){
    if(!plan?.digest)return;
    setBusy('confirm');setError('');setMessage('');
    try{
      const result=await post('/library/actions',{...actionBody(),digest:plan.digest});
      setPlan(null);setSelected(new Set());
      setMessage('已排队 '+(result.tasks?.length||0)+' 项');
    }catch(exception){
      if(exception.code==='LIBRARY_PLAN_CHANGED')setPlan(null);
      failed(exception);
    }
    finally{setBusy('');}
  }

  function startFollowing(group){
    const groupIds=group?[String(group.subgroupId)]:filters.group_ids;
    const sourceGroup=groupIds.length===1?groupIds[0]:'0';
    setFollow({
      groupIds,sourceGroup,rules:{...filters,group_ids:groupIds,wait_minutes:sourceGroup==='0'?5:0},
      subscription:currentSubscription(sourceGroup),
    });
  }

  function go(path){
    close();
    window.location.hash=path;
    window.scrollTo({top:0,left:0,behavior:'instant'});
  }

  if(follow)return <FollowModal
    work={{...work,mikan_id:bid}} subscription={follow.subscription}
    initialGroups={follow.groupIds} initialRules={follow.rules}
    sourceGroupId={follow.sourceGroup} close={()=>setFollow(null)}
    onSaved={()=>setFollow(null)}
  />;

  return <Modal title={title||data?.title||'选择资源'} close={close} footer={<>
    <button className="secondary-button m115-follow-entry" disabled={!!busy||!bid} onClick={()=>startFollowing()}>
      {currentSubscription(filters.group_ids.length===1?filters.group_ids[0]:'0')?'管理追新':'追新'}
    </button>
    {bid&&<button className="secondary-button" disabled={!!busy||!matching.length} onClick={previewLibrary}>
      {busy==='preview'?'匹配中…':chosen.length?'预览所选入库':'预览入库'}
    </button>}
    {!!plan?.items?.length&&<button className="primary-button" disabled={!!busy} onClick={confirmLibrary}>
      {busy==='confirm'?'排队中…':'确认入库'}
    </button>}
    {chosen.length>0&&<button className="primary-button" disabled={!!busy} onClick={()=>transfer(chosen)}>
      {busy==='transfer'?'提交中…':'批量转存（'+chosen.length+'）'}
    </button>}
    <button className="secondary-button" onClick={()=>go('/requests')}>任务</button>
  </>}>
    {error&&<Notice error={error}/>}
    {message&&<Notice>{message}</Notice>}
    {login&&<button className="primary-button" onClick={()=>go('/login')}>登录</button>}
    {data?.source_required&&<>
      <form className="m115-search" onSubmit={search}>
        <input aria-label="搜索资源" value={term} onChange={event=>setTerm(event.target.value)}/>
        <button className="primary-button" disabled={!!busy}>搜索</button>
      </form>
      <div className="m115-source-list">{sources.map(source=><button
        className="m115-source" key={source.mikan_id||source.bangumiId}
        onClick={()=>load(String(source.mikan_id||source.bangumiId))}
      >{displayTitle(source)}</button>)}</div>
    </>}
    {!!groups.length&&<>
      <section className="m115-resource-filters" aria-label="资源筛选">
        <FilterChoices label="分辨率" values={filters.resolutions} choices={resolutions}
          onChange={values=>changeFilters('resolutions',values)}/>
        <FilterChoices label="字幕" values={filters.versions} choices={subtitleVersions}
          onChange={values=>changeFilters('versions',values)}/>
        <details className="m115-resource-group-filter">
          <summary>字幕组{filters.group_ids.length?' · 已选 '+filters.group_ids.length+' 组':' · 不限'}</summary>
          <button type="button" className="secondary-button" onClick={()=>changeFilters('group_ids',[])}>全部字幕组</button>
          <div className="m115-group-options">{groups.map(group=><label key={group.subgroupId}>
            <input type="checkbox" checked={filters.group_ids.includes(String(group.subgroupId))}
              onChange={event=>changeFilters('group_ids',event.target.checked
                ?[...filters.group_ids,String(group.subgroupId)]
                :filters.group_ids.filter(id=>id!==String(group.subgroupId)))}/>
            <span>{group.subgroupName}</span>
          </label>)}</div>
        </details>
      </section>
      <section className="m115-library-actions">
        <div className="m115-heading">
          <h3>入库</h3>
          <select aria-label="入库动作" value={action} onChange={event=>{
            setAction(event.target.value);setPlan(null);
          }}>
            <option value="missing">补缺集</option>
            <option value="complete">入库全集</option>
            <option value="upgrade">洗版</option>
          </select>
        </div>
        {plan&&<div className="m115-library-preview">
          <strong>{plan.items?.length?'将处理 '+plan.items.length+' 项':'暂无匹配资源'}</strong>
          {plan.missing_episodes?.length>0&&<span>缺集 {plan.missing_episodes.join('、')}</span>}
          {plan.items?.map(item=><span key={item.release_id}>
            {item.title} · {(item.episodes||[]).join('、')} 集
          </span>)}
        </div>}
      </section>
    </>}
    {shown.map(group=>{
      const ids=group.resources.map(releaseId).filter(Boolean);
      const allSelected=ids.length>0&&ids.every(id=>selected.has(id));
      const following=currentSubscription(group.subgroupId);
      const isExpanded=expanded.has(String(group.subgroupId));
      const rows=isExpanded?group.resources:group.resources.slice(0,3);
      return <section className="m115-resource-group" key={group.subgroupId} aria-label={group.subgroupName}>
        <div className="m115-resource-group-heading">
          <h3>{group.subgroupName}<small>{group.resources.length} 项</small></h3>
          <div className="m115-resource-group-actions">
            <button type="button" className="secondary-button" aria-pressed={allSelected}
              disabled={!!busy||!ids.length} onClick={()=>selectGroup(group)}>
              {allSelected?'取消全选':'全选'}
            </button>
            <button type="button" className="secondary-button" disabled={!!busy}
              onClick={()=>startFollowing(group)}>{following?'管理追新':'追新'}</button>
          </div>
        </div>
        {rows.map(resource=>{
          const id=releaseId(resource);
          return <article className="m115-resource" key={id||resource.title}>
            <label className="m115-resource-select">
              <input type="checkbox" aria-label={'选择 '+resource.title} checked={selected.has(id)}
                disabled={!id||!!busy} onChange={()=>toggle(resource)}/>
            </label>
            <div><strong>{resource.title}</strong><ReleaseInfo release={resource.release}/>
              <small>{[resource.size,resource.pubDate].filter(Boolean).join(' · ')}</small>
            </div>
            <button className="primary-button" disabled={!!busy||!id} onClick={()=>transfer([id])}>转存</button>
          </article>;
        })}
        {group.resources.length>3&&<button type="button" className="secondary-button m115-resource-expand"
          aria-expanded={isExpanded} onClick={()=>setExpanded(previous=>{
            const next=new Set(previous);
            isExpanded?next.delete(String(group.subgroupId)):next.add(String(group.subgroupId));
            return next;
          })}>{isExpanded?'收起资源':'查看全部 '+group.resources.length+' 项'}</button>}
      </section>;
    })}
    {busy==='load'&&<Notice>加载中…</Notice>}
    {data&&!data.source_required&&!shown.length&&busy!=='load'&&<p className="muted">
      {groups.length?'暂无匹配资源':'暂无资源'}
    </p>}
  </Modal>;
}

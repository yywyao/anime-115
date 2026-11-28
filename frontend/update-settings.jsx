import {api,post} from './api.js';
import {Modal,Notice} from './components.jsx';

const phases={checking:'检查中',downloading:'下载中',ready:'待更新',waiting:'等待切换',installing:'更新中',current:'已是最新',available:'有新版本',error:'更新失败',rolled_back:'已恢复原版本'};
const ongoing=new Set(['checking','downloading','waiting','installing']);
export function UpdateSettings(){
 const {useState,useEffect}=window.Mikan115Runtime.React;
 const [status,setStatus]=useState(null),[error,setError]=useState(''),[confirm,setConfirm]=useState(''),[busy,setBusy]=useState(false);
 async function load(){try{setStatus(await api('/settings/updates'));setError('');}catch(e){setError(e.message);}}
 useEffect(()=>{load();const timer=setInterval(load,3000);return()=>clearInterval(timer);},[]);
 async function change(body){setBusy(true);setError('');try{setStatus(await post('/settings/updates',body));setConfirm('');}catch(e){setError(e.message);}finally{setBusy(false);}}
 const disabled=busy||ongoing.has(status?.phase);
 return <section className="m115-card"><div className="m115-heading"><h2>系统更新</h2><span className="m115-pill">{phases[status?.phase]||'未检查'}</span></div>
  {error&&<Notice error={error}/>} {status?.error&&<Notice error={status.error}/>}
  {status&&<><dl className="m115-status"><div><dt>当前版本</dt><dd>{status.current==='bundled'?'内置版本':status.current?.slice(0,12)}</dd></div>{status.latest&&<div><dt>最新版本</dt><dd>{status.latest.slice(0,12)}</dd></div>}{status.changed_files>0&&<div><dt>增量文件</dt><dd>{status.downloaded_files??0} / {status.changed_files}</dd></div>}</dl>
   <div className="m115-actions"><button className="secondary-button" disabled={disabled} onClick={()=>change({action:'check'})}>检查更新</button>{status.available&&!status.staged&&<button className="secondary-button" disabled={disabled} onClick={()=>change({action:'download'})}>下载更新</button>}{status.staged&&<button className="primary-button" disabled={disabled||!status.supervised} onClick={()=>change({action:'apply'})}>立即更新</button>}{status.rollback_available&&<button className="secondary-button" disabled={disabled||!status.supervised} onClick={()=>setConfirm('rollback')}>恢复上一版</button>}</div>
   <label className="m115-checkbox"><input type="checkbox" checked={status.auto_check} disabled={disabled} onChange={e=>change({auto_check:e.target.checked})}/>自动检查更新</label>
   <label className="m115-checkbox"><input type="checkbox" checked={status.auto_update} disabled={disabled||!status.supervised} onChange={e=>e.target.checked?setConfirm('automatic'):change({auto_update:false})}/>自动更新</label>
   {!status.supervised&&<Notice>更新服务未启动，请通过 run.py 重新启动。</Notice>}
  </>}
  {confirm&&<Modal title={confirm==='rollback'?'恢复上一版':'开启自动更新'} close={()=>setConfirm('')} footer={<button className="primary-button" disabled={busy} onClick={()=>change(confirm==='rollback'?{action:'rollback'}:{auto_check:true,auto_update:true})}>确认</button>}><p>{confirm==='rollback'?'恢复程序版本，保留当前账号、配置和文件。':'有新版本时自动下载并重启网站。'}</p></Modal>}
 </section>;
}

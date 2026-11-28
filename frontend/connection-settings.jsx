import {api,post} from './api.js';
const R=()=>window.Mikan115Runtime.React;

export function ConnectionSettings({connection,onChange}){
 const {useState,useEffect}=R();
 const [cookie,setCookie]=useState(''),[name,setName]=useState('anime'),[busy,setBusy]=useState(''),[cookieError,setCookieError]=useState(''),[directoryError,setDirectoryError]=useState(''),[message,setMessage]=useState('');
 useEffect(()=>{if(connection)setName(connection.directory_name||'anime');},[connection]);
 async function refresh(){try{onChange(await api('/settings/connection'));}catch{}}
 async function verify(e){
  e.preventDefault();setBusy('verify');setCookieError('');setMessage('');
  try{const data=await post('/settings/connection/verify',{...(cookie.trim()?{cookie:cookie.trim()}:{}),cookie_source:'third_party'});onChange(data);setCookie('');setMessage('CK 已验证');}
  catch(error){await refresh();setCookieError(error.message);}finally{setBusy('');}
 }
 async function save(e){
  e.preventDefault();setBusy('directory');setDirectoryError('');setMessage('');
  try{const data=await post('/settings/connection/directory',{name:name.trim()||'anime'});onChange(data);setMessage('已连接');}
  catch(error){await refresh();setDirectoryError(error.message);}finally{setBusy('');}
 }
 return <section className="m115-card m115-connection">
  <div className="m115-heading"><h2>115</h2><span className={'m115-pill '+(connection?.ready?'ready':'')}>{connection?.ready?'已连接':connection?.cookie_verified?'待设置目录':'未连接'}</span></div>
  {connection?.account_name&&<dl className="m115-status"><div><dt>账号</dt><dd>{connection.account_name}</dd></div>{connection.transfer_folder&&<div><dt>当前目录</dt><dd>{connection.transfer_folder}</dd></div>}<div><dt>AList</dt><dd>{connection.alist_managed?'自动配置':connection.alist_configured?'已配置':'待配置'}</dd></div></dl>}
  <form className="m115-form" onSubmit={verify}>
   <label>第三方 115 CK<input type="password" name="115-cookie" value={cookie} onChange={e=>setCookie(e.target.value)} placeholder={connection?.cookie_configured?'已保存':'UID=…; CID=…; SEID=…; KID=…'} autoComplete="off" spellCheck="false" required={!connection?.cookie_configured}/></label>
   {(cookieError||connection?.cookie_error)&&<p role="alert" className="m115-notice error">{cookieError||connection.cookie_error}</p>}
   <div className="m115-inline"><button className="secondary-button" disabled={!!busy}>{busy==='verify'?'验证中…':'验证 CK'}</button>{connection?.cookie_verified&&<span className="m115-pill ready">CK 已验证</span>}</div>
  </form>
  <form className="m115-form m115-directory-form" onSubmit={save}>
   <label>转存目录<input name="115-directory" value={name} onChange={e=>setName(e.target.value)} placeholder="anime" maxLength="100" autoComplete="off" disabled={!connection?.cookie_verified||!!busy}/></label>
   {(directoryError||connection?.connection_error)&&<p role="alert" className="m115-notice error">{directoryError||connection.connection_error}</p>}
   <div className="m115-inline"><button className="primary-button" disabled={!connection?.cookie_verified||!!busy}>{busy==='directory'?'保存中…':'保存目录'}</button><button type="button" className="secondary-button" disabled={!connection?.cookie_verified||!!busy} onClick={()=>setName('anime')}>使用默认</button></div>
  </form>
  {message&&<p role="status" className="m115-notice">{message}</p>}
 </section>;
}

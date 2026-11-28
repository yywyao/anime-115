import {api,post} from './api.js';

export function ServiceSettings({service,onConnection}){
  const {useState,useEffect}=window.Mikan115Runtime.React;
  const name=service==='alist'?'AList':'Sonarr';
  const [status,setStatus]=useState(null);
  const [mode,setMode]=useState('automatic');
  const [url,setUrl]=useState('');
  const [username,setUsername]=useState('admin');
  const [password,setPassword]=useState('');
  const [token,setToken]=useState('');
  const [error,setError]=useState('');
  const [loadError,setLoadError]=useState('');
  const [message,setMessage]=useState('');
  const [busy,setBusy]=useState(false);

  function apply(value){
    setLoadError('');
    setStatus(value);
    setMode(value.mode||'automatic');
    setUrl(value.url||'');
    setUsername(value.username||'admin');
  }

  useEffect(()=>{
    const controller=new AbortController();
    let initial=true,timer=null;
    async function load(){
      try{
        const value=await api('/settings/'+service,{signal:controller.signal});
        if(controller.signal.aborted)return;
        setLoadError('');
        if(initial){apply(value);initial=false;}
        else setStatus(value);
        if(!value.ready)timer=setTimeout(load,8000);
      }catch(e){
        if(e.name==='AbortError')return;
        setLoadError(e.message);
        timer=setTimeout(load,8000);
      }
    }
    load();
    return()=>{controller.abort();if(timer)clearTimeout(timer);};
  },[service]);

  async function save(event){
    event.preventDefault();
    setBusy(true);setError('');setMessage('');
    try{
      let body={mode};
      if(mode==='custom'){
        body.url=url.trim();
        if(service==='alist')Object.assign(body,{username:username.trim(),password,token:token.trim()});
        else Object.assign(body,{api_key:token.trim(),api_version:'auto'});
      }
      const value=await post('/settings/'+service,body);
      apply(value);setPassword('');setToken('');
      setMessage('已保存');
      if(value.connection)onConnection?.(value.connection);
    }catch(e){setError(e.message);}
    finally{setBusy(false);}
  }

  async function check(){
    setBusy(true);setError('');setMessage('');
    try{
      const value=await post('/settings/'+service,{check:true});
      setStatus(value);
      if(value.ready)setMessage('连接正常');
    }catch(e){setError(e.message);}
    finally{setBusy(false);}
  }

  return <section className="m115-card" aria-label={name+' 连接'}>
    <div className="m115-heading">
      <h2>{name}</h2>
      <span className={'m115-pill '+(status?.ready?'ready':'')}>
        {status===null?'检查中…':status.ready?'已连接':'未就绪'}
      </span>
    </div>
    <form className="m115-form" onSubmit={save}>
      <label>连接方式
        <select aria-label={name+' 连接方式'} value={mode} onChange={e=>{
          setMode(e.target.value);setError('');setMessage('');
        }}>
          <option value="automatic">自动</option>
          <option value="custom">自定义</option>
        </select>
      </label>
      {mode==='custom'&&<>
        <label>地址<input aria-label={name+' 地址'} type="url" value={url}
          onChange={e=>setUrl(e.target.value)} autoComplete="off" required/></label>
        {service==='alist'&&<>
          <label>用户名<input aria-label="AList 用户名" value={username}
            onChange={e=>setUsername(e.target.value)} autoComplete="off" required/></label>
          <label>管理员密码<input aria-label="AList 管理员密码" type="password" value={password}
            onChange={e=>setPassword(e.target.value)} autoComplete="new-password"/></label>
        </>}
        <label>{service==='alist'?'管理令牌':'API Key'}
          <input aria-label={name+(service==='alist'?' 管理令牌':' API Key')} type="password" value={token}
            onChange={e=>setToken(e.target.value)} autoComplete="new-password"/>
        </label>
      </>}
      <div className="m115-actions">
        <button className="primary-button" disabled={busy||status===null}>
          {busy?'处理中…':mode==='automatic'?'使用自动配置':'保存'}
        </button>
        <button type="button" className="secondary-button" disabled={busy||status===null} onClick={check}>检查连接</button>
      </div>
    </form>
    {(error||loadError||status?.error)&&<p role="alert" className="m115-notice error">{error||loadError||status.error}</p>}
    {message&&<p role="status" className="m115-notice">{message}</p>}
  </section>;
}

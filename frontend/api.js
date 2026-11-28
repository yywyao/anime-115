const errors={LOGIN_REQUIRED:'请先登录',ADMIN_REQUIRED:'需要管理员权限',CSRF_REQUIRED:'请刷新后重试',CONNECTION_REQUIRED:'请先连接 115',COOKIE_EXPIRED:'CK 已失效，请更新',ALIST_CREDENTIALS_REQUIRED:'请填写 Alist 认证信息',ALIST_STORAGE_CHANGED:'连接已变更，请重新绑定',ALIST_META_CHANGED:'连接已变更，请重新绑定',ALIST_COOKIE_MISMATCH:'CK 不一致，请重新绑定',ALIST_ACCOUNT_MISMATCH:'网盘账号不一致，请重新绑定',FOLDER_SCOPE_MISMATCH:'目录已变更，请保存目录设置',SUBMIT_RESULT_UNKNOWN:'提交结果待核实，请刷新状态',AMBIGUOUS_SUBMISSION:'结果待核实，请刷新状态',CLOUD_TASK_FAILED:'转存失败，请检查配额或更换资源',FILES_NOT_READY:'文件尚未就绪',INVALID_MAGNET:'磁力链接无效',UPSTREAM_UNAVAILABLE:'连接超时，请重试',UPSTREAM_HTTP_ERROR:'连接失败，请重试'};
export const errorText=(code,message)=>errors[code]||message||'操作失败，请重试';
export async function api(path,options={}){
 let response,body;
 try{response=await fetch('/api'+path,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...(window.Mikan115.csrf?{'X-CSRF-Token':window.Mikan115.csrf}:{}),...(options.headers||{})}});}catch(e){if(e.name==='AbortError')throw e;throw new Error('连接失败，请重试');}
 try{body=await response.json();if(!body||typeof body!=='object'||Array.isArray(body))throw new Error();}catch(e){if(e.name==='AbortError')throw e;throw new Error('加载失败，请重试');}
 if(body.csrf_token)window.Mikan115.csrf=body.csrf_token;
 if(!response.ok){const e=new Error(errorText(body.code,body.error||body.message));e.code=body.code;e.status=response.status;throw e;}return body;
}
export const post=(path,data={})=>api(path,{method:'POST',body:JSON.stringify(data)});

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {parse} from 'acorn';
import {transform} from 'esbuild';

const dir=path.dirname(fileURLToPath(import.meta.url));
const sourcePath=process.argv[2];
if(!sourcePath)throw new Error('Pass the isolated reference frontend bundle path.');
const source=fs.readFileSync(sourcePath,'utf8');
if(crypto.createHash('sha256').update(source).digest('hex')!=='9bff4ebc86ff7ef42c19a3e3b6c70e98d58801b840e654b6abe152ae20090778')throw new Error('Reference bundle changed; review the adapter before rebuilding.');
const tree=parse(source,{ecmaVersion:'latest',sourceType:'module'});let edits=[];let detailPatched=0,modalRemoved=0,routerPatched=0,sourceNotice=0,posterPatched=0;
const walk=(node,visit)=>{if(!node||typeof node!=='object')return;visit(node);for(const [key,value] of Object.entries(node)){if(key==='start'||key==='end')continue;if(Array.isArray(value))value.forEach(x=>walk(x,visit));else if(value&&typeof value==='object')walk(value,visit);}};
const apiKeys=new Set(['auth','login','logout','changePassword','weekdays','homepage','seasons','season','search','detail','detailResources','catalogWork','catalogSeries','favorite','setFavorite','tmdbTitle','tmdbSearch','tmdbPerson','library','quota']);
for(const node of tree.body){
 if(node.type==='FunctionDeclaration'&&['Ig','Wg'].includes(node.id.name))walk(node.body,current=>{
  if(current.type!=='CallExpression'||current.arguments[1]?.type!=='ObjectExpression')return;
  const children=current.arguments[1].properties.find(p=>p.key.name==='children')?.value;
  const texts=children?.type==='ArrayExpression'?children.elements.filter(x=>x?.type==='Literal').map(x=>x.value):[children?.value];
  if(texts.includes('我的观影记录')||texts.includes('登录后可使用求片记录和个人设置。'))edits.push([current.start,current.end,'null']);
 });
 if(node.type==='FunctionDeclaration'&&node.id.name==='dy')walk(node.body,current=>{
  if(current.type==='Literal'&&['可以尝试更短的标题或不同关键词。','请重新加载后再试。'].includes(current.value))edits.push([current.start,current.end,'null']);
 });
 if(node.type==='FunctionDeclaration'&&node.id.name==='Ha'){
  edits.push([node.start,node.end,`function Ha({src:c,fallbackSrc:o="",alt:f,className:r="",allowNonVertical:y=!1,eager:_=!1}){
   const missing=e=>{e.hidden=true;e.classList.add('poster-missing');e.parentElement?.classList.add('is-missing');};
   return n.jsx('img',{className:'poster-media '+r,src:c||o||im,alt:f||'作品海报',loading:_||y?'eager':'lazy',
    onLoad:event=>{const e=event.currentTarget;if(e.naturalWidth>0&&e.naturalHeight>0){e.hidden=false;e.classList.remove('poster-missing');e.parentElement?.classList.remove('is-missing');e.dataset.checked='1';e.dataset.posterShape=e.naturalWidth<e.naturalHeight?'vertical':'source';}else missing(e);},
    onError:event=>{const e=event.currentTarget;if(o&&e.dataset.fallback!=='1'&&e.src!==o){e.dataset.fallback='1';e.src=o;}else missing(e);}});
  }`]);posterPatched++;
 }
 if(node.type==='FunctionDeclaration'&&node.id.name==='ry')walk(node.body,n=>{
  if(n.type==='CallExpression'&&n.arguments[0]?.value==='main'&&n.arguments[1]?.type==='ObjectExpression'){
   const props=n.arguments[1].properties;const clazz=props.find(p=>p.key.name==='className');const children=props.find(p=>p.key.name==='children');
   if(clazz?.value?.value?.includes('product-home')&&children?.value?.type==='ArrayExpression'){
    edits.push([children.value.start+1,children.value.start+1,'n.jsx(window.Mikan115.SourceNotice,{message:j.data?.source_notice}),']);sourceNotice++;
   }
  }
 });
 if(node.type==='FunctionDeclaration'&&node.id.name==='_y'){
  walk(node.body,n=>{
   if(n.type==='VariableDeclarator'&&n.id?.name==='G'){
    edits.push([n.init.start,n.init.end,'n.jsx(window.Mikan115.TransferButton,{work:j})']);detailPatched++;
   }
   if(n.type==='VariableDeclarator'&&n.id?.name==='ne')edits.push([n.init.start,n.init.end,'null']);
   if(n.type==='CallExpression'&&n.callee.type==='MemberExpression'&&['jsx','jsxs'].includes(n.callee.property.name)&&n.arguments[0]?.name==='hy'){
    edits.push([n.start,n.end,'null']);modalRemoved++;
   }
  });
 }
 if(node.type==='FunctionDeclaration'&&node.id.name==='ky'){
  edits.push([node.start,node.end,`function ky(){
   const [route,go]=$g();const state=Ce(()=>le.auth().catch(()=>({authenticated:false})),[]);
   const user=state.data?.user;let body;
   if(route.startsWith('/login')||route.startsWith('/setup'))body=n.jsx(window.Mikan115.Login,{go});
   else if(route.startsWith('/search'))body=n.jsx(window.Mikan115.Search,{go});
   else if(route.startsWith('/detail/'))body=n.jsx(window.Mikan115.Detail,{id:decodeURIComponent(route.split('/')[2]?.split('?')[0]||''),go});
   else if(route.startsWith('/library/'))body=n.jsx(window.Mikan115.Detail,{id:decodeURIComponent(route.split('/')[2]||''),go});
   else if(route.startsWith('/person/'))body=n.jsx(window.Mikan115.Person,{id:route.split('/')[2],go});
   else if(route.startsWith('/settings'))body=user?n.jsx(window.Mikan115.Settings,{user,go}):n.jsx(window.Mikan115.Login,{go});
   else if(route.startsWith('/admin'))body=user?.is_admin?n.jsx(window.Mikan115.Admin,{user,go}):n.jsx(window.Mikan115.Login,{go});
   else if(route.startsWith('/following'))body=user?n.jsx(window.Mikan115.FollowManager,{user}):n.jsx(window.Mikan115.Login,{go});
   else if(route.startsWith('/requests')||route.startsWith('/library')||route.startsWith('/transfers'))body=user?n.jsx(window.Mikan115.Transfers,{user,go,filesOnly:route.startsWith('/library')}):n.jsx(window.Mikan115.Login,{go});
   else body=n.jsx(window.Mikan115.Home,{go});
   const detail=route.startsWith('/detail/')||route.startsWith('/library/')||route.startsWith('/person/');
   return n.jsxs(n.Fragment,{children:[!detail&&n.jsx(Ig,{route,go,user,authenticated:!!state.data?.authenticated}),body,!detail&&n.jsx(Wg,{go,user,authenticated:!!state.data?.authenticated})]});
  }`]);routerPatched++;
 }
 if(node.type==='VariableDeclaration')for(const d of node.declarations){
  if(d.id?.name==='le'&&d.init.type==='ObjectExpression'){
   const props=d.init.properties.filter(p=>apiKeys.has(p.key.name)).map(p=>source.slice(p.start,p.end));
   edits.push([d.init.start,d.init.end,'{'+props.join(',')+'}']);
  }
 }
}
if(detailPatched!==1||modalRemoved!==1||routerPatched!==1||sourceNotice!==1||posterPatched!==1)throw new Error('Expected UI adapter locations are missing.');
edits.sort((a,b)=>b[0]-a[0]);let patched=source;
for(const [start,end,text] of edits)patched=patched.slice(0,start)+text+patched.slice(end);
patched=patched.replaceAll('/auth/emby/login','/auth/login').replaceAll('emby_url','download_url').replaceAll('emby_user_id','local_user_id').replaceAll('emby_username','username');
patched=patched.replaceAll('Emby 用户名','本地用户名').replaceAll('Emby ID','本地账号 ID').replaceAll('Emby 账户','本地账户').replaceAll('Emby','本地媒体');
patched=patched.replaceAll('求片','转存').replaceAll('媒体库','转存文件');
patched=patched.replaceAll('我的转存','转存任务').replaceAll('转存文件','文件').replaceAll('个人设置','设置').replaceAll('管理后台','后台');
const jsonPoint='data=await response.json()';
if(!patched.includes(jsonPoint))throw new Error('API response adapter location missing');
patched=patched.replace(jsonPoint,'data=await response.json();if(data.csrf_token)window.Mikan115.csrf=data.csrf_token');
const headerPoint='headers:form?{...o.headers||{}}:{"Content-Type":"application/json",...o.headers||{}}';
if(!patched.includes(headerPoint))throw new Error('API CSRF adapter location missing');
patched=patched.replace(headerPoint,'headers:{...(form?{}:{"Content-Type":"application/json"}),...(window.Mikan115.csrf?{"X-CSRF-Token":window.Mikan115.csrf}:{}),...(o.headers||{})}');
const mountPoint='Kg.createRoot(document.getElementById("root"))';
if(!patched.includes(mountPoint))throw new Error('React mount bridge location missing');
patched=patched.replace(mountPoint,'window.Mikan115Runtime={React:V,Poster:Ha,Hero:bm,model:pm,createPortal:xt.createPortal};'+mountPoint);
patched='import "/static/mikan115-ui.js";\n'+patched;
// Bundling a module with no exports removes unreachable legacy account/admin/media flows.
const result=await transform(patched,{loader:'js',format:'esm',minify:true,treeShaking:true,legalComments:'eof'});
if(/emby/i.test(result.code))throw new Error('Unremoved media-server references in active UI');
parse(result.code,{ecmaVersion:'latest',sourceType:'module'});
fs.writeFileSync(path.join(dir,'shell.js'),result.code);
fs.writeFileSync(path.join(dir,'ADAPTER-REPORT.json'),JSON.stringify({baseline_sha256:'9bff4ebc86ff7ef42c19a3e3b6c70e98d58801b840e654b6abe152ae20090778',detail_transfer_action:detailPatched,legacy_request_modal_removed:modalRemoved,local_router:routerPatched,snapshot_notice:sourceNotice,poster_rendering:posterPatched,original_bytes:Buffer.byteLength(source),adapted_bytes:Buffer.byteLength(result.code),real_login_performed:false},null,2));
console.log('LEGACY_UI_ADAPTED',Buffer.byteLength(result.code));

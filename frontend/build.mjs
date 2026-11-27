import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {build,transform} from 'esbuild';
const dir=path.dirname(fileURLToPath(import.meta.url));const root=path.dirname(dir);
fs.mkdirSync(path.join(root,'static/assets'),{recursive:true});
await build({entryPoints:[path.join(dir,'components.jsx')],outfile:path.join(root,'static/mikan115-ui.js'),
 bundle:true,format:'esm',minify:true,jsxFactory:'window.Mikan115Runtime.React.createElement',jsxFragment:'window.Mikan115Runtime.React.Fragment',legalComments:'eof'});
const uiPath=path.join(root,'static/mikan115-ui.js');const ui=fs.readFileSync(uiPath,'utf8').replace('href:g.download_url},"\\u4E0B\\u8F7D"','href:g.download_url},"\\u4E0B\\u8F7D\\u5230\\u672C\\u673A"');fs.writeFileSync(uiPath,ui);
let shell=fs.readFileSync(path.join(dir,'shell.js'),'utf8');
if(!/onClick:\(\)=>\w+\("\/following"\)/.test(shell)){
 const nav=/([\w$]+)\.jsx\("button",\{className:([\w$]+)==="requests"\?"active":"",onClick:\(\)=>([\w$]+)\("\/requests"\),children:"[^"]+"\}\)/;
 if(!nav.test(shell))throw new Error('Following navigation adapter is missing');
 shell=shell.replace(nav,(button,jsx,route,go)=>`${jsx}.jsx("button",{className:${route}==="following"?"active":"",onClick:()=>${go}("/following"),children:"追新"}),${button}`);
}
const result=await transform(shell,{loader:'js',format:'esm',minify:true,legalComments:'eof'});
fs.writeFileSync(path.join(root,'static/assets/mikan115-app.js'),result.code);
const css=[fs.readFileSync(path.join(dir,'mikan115.css'),'utf8'),fs.readFileSync(path.join(dir,'review.css'),'utf8')].map(value=>value.replace(/\r\n/g,'\n')).join('\n');
fs.writeFileSync(path.join(root,'static/mikan115.css'),css);
console.log('FRONTEND_BUILT',fs.statSync(path.join(root,'static/mikan115-ui.js')).size,result.code.length);

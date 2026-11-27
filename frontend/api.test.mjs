import assert from 'node:assert/strict';
import {test,beforeEach,afterEach} from 'node:test';
import {api,post} from './api.js';
const nativeFetch=globalThis.fetch;
beforeEach(()=>{globalThis.window={Mikan115:{csrf:''}};});
afterEach(()=>{globalThis.fetch=nativeFetch;delete globalThis.window;});
test('network failures show a concise recoverable Chinese error',async()=>{
 globalThis.fetch=async()=>{throw new TypeError('Failed to fetch');};
 await assert.rejects(api('/auth/config'),e=>e.message==='连接失败，请重试');
});
test('invalid JSON and unexpected response shapes do not leak parser details',async()=>{
 for(const value of ['invalid',null,[],false]){
  globalThis.fetch=async()=>({ok:false,json:async()=>{if(value==='invalid')throw new SyntaxError('Unexpected private response');return value;}});
  await assert.rejects(api('/auth/config'),e=>e.message==='加载失败，请重试');
 }
});
test('cancelled requests preserve AbortError for route changes',async()=>{
 const aborted=Object.assign(new Error('aborted'),{name:'AbortError'});
 globalThis.fetch=async()=>{throw aborted;};
 await assert.rejects(api('/season'),e=>e===aborted);
});
test('cancelled response bodies preserve AbortError',async()=>{
 const aborted=Object.assign(new Error('aborted'),{name:'AbortError'});
 globalThis.fetch=async()=>({ok:true,json:async()=>{throw aborted;}});
 await assert.rejects(api('/season'),e=>e===aborted);
});
test('successful retry stores CSRF and sends it with JSON mutations',async()=>{
 let calls=[];globalThis.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({csrf_token:'artificial-csrf',ok:true})};};
 await api('/auth/config');await post('/favorite',{canonicalId:'artificial-title',favorite:true});
 assert.equal(calls[1].options.headers['X-CSRF-Token'],'artificial-csrf');
 assert.equal(calls[1].options.credentials,'same-origin');assert.equal(calls[1].options.method,'POST');
 assert.deepEqual(JSON.parse(calls[1].options.body),{canonicalId:'artificial-title',favorite:true});
});
test('HTTP authorization failures retain status and use product copy',async()=>{
 globalThis.fetch=async()=>({ok:false,status:401,json:async()=>({code:'LOGIN_REQUIRED',error:'Internal session explanation'})});
 await assert.rejects(api('/transfers'),e=>e.message==='请先登录'&&e.status===401&&e.code==='LOGIN_REQUIRED');
});

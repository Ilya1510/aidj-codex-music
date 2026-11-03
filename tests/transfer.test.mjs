import test from 'node:test';
import assert from 'node:assert/strict';
import {LocalTransfer,transferKey,seal,unseal} from '../miniapp/transfer.mjs';
const token='test_local_token_'.repeat(3);
const encode=new TextEncoder(),decode=new TextDecoder();
test('default browser fetch keeps the Window receiver',async()=>{
  const original=globalThis.fetch,key=await transferKey(token);
  try{
    globalThis.fetch=async function(url,options){
      assert.equal(this,globalThis,'browser fetch rejects an unbound receiver');
      const path=new URL(url).pathname;
      const reply=await seal(key,'reply:'+path,encode.encode(JSON.stringify({missing:[],existing:[]})));
      return new Response(reply.body,{headers:{'X-AIDJ-IV':reply.iv}});
    };
    await new LocalTransfer({port:12345,token}).request('/manifest',{tracks:[]});
  }finally{globalThis.fetch=original;}
});
test('phone transfer is encrypted and authenticates each route',async()=>{
  const key=await transferKey(token),plaintext=encode.encode('PRIVATE_AUDIO_BYTES');
  const encrypted=await seal(key,'/file/test',plaintext);
  assert(!Buffer.from(encrypted.body).includes(Buffer.from(plaintext)));
  assert.deepEqual(new Uint8Array(await unseal(key,'/file/test',encrypted.iv,encrypted.body)),plaintext);
  await assert.rejects(unseal(key,'/manifest',encrypted.iv,encrypted.body));
  await assert.rejects(unseal(await transferKey('wrong secret'),'/file/test',encrypted.iv,encrypted.body));
});
test('transfer copies cached bytes locally, skips existing files, and marks only authenticated acknowledgements',async()=>{
  const key=await transferKey(token);let copied=0;const complete=[];
  const tracks=[{md5:'already',bytes:5},{md5:'missing',bytes:5}];
  const fetcher=async(url,options)=>{
    assert(url.startsWith('http://127.0.0.1:12345/'));
    assert.equal(options.credentials,'omit');assert(!JSON.stringify(options.headers).includes(token));
    const path=new URL(url).pathname;
    const raw=await unseal(key,path,options.headers['X-AIDJ-IV'],options.body);
    let reply;
    if(path==='/manifest'){assert.equal(JSON.parse(decode.decode(raw)).tracks.length,2);reply={missing:['missing'],existing:['already']};}
    else if(path==='/finish')reply={ok:true};
    else{assert.equal(decode.decode(raw),'hello');copied++;reply={ok:true,md5:'missing'};}
    const sealed=await seal(key,'reply:'+path,encode.encode(JSON.stringify(reply)));
    return new Response(sealed.body,{headers:{'X-AIDJ-IV':sealed.iv}});
  };
  const transfer=new LocalTransfer({port:12345,token},fetcher);
  const result=await transfer.run(tracks,async md5=>{assert.equal(md5,'missing');return new Blob(['hello']);},{completed:t=>complete.push(t.md5)});
  assert.equal(copied,1);assert.deepEqual(complete,['already','missing']);assert.equal(result.done,2);
});
test('an unauthenticated local responder cannot mark files complete or trigger deletion',async()=>{
  let completed=0;
  const transfer=new LocalTransfer({port:12345,token},async()=>new Response(JSON.stringify({missing:[],existing:['anything']})));
  await assert.rejects(transfer.run([{md5:'anything'}],async()=>new Blob(),{completed:()=>completed++}));
  assert.equal(completed,0);
});

const encoder=new TextEncoder(),decoder=new TextDecoder();
export async function transferKey(token){
  const digest=await crypto.subtle.digest('SHA-256',encoder.encode(token));
  return crypto.subtle.importKey('raw',digest,'AES-GCM',false,['encrypt','decrypt']);
}
export const encodeIV=iv=>btoa(String.fromCharCode(...iv)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
export const decodeIV=value=>Uint8Array.from(atob(value.replaceAll('-','+').replaceAll('_','/')),c=>c.charCodeAt(0));
export async function seal(key,path,bytes){
  const iv=crypto.getRandomValues(new Uint8Array(12));
  const body=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:encoder.encode(path)},key,bytes);
  return {iv:encodeIV(iv),body};
}
export async function unseal(key,path,iv,bytes){
  return crypto.subtle.decrypt({name:'AES-GCM',iv:decodeIV(iv),additionalData:encoder.encode(path)},key,bytes);
}
export class LocalTransfer {
  constructor(descriptor,fetcher=globalThis.fetch.bind(globalThis)){
    if(!Number.isInteger(descriptor.port)||descriptor.port<1024||descriptor.port>65535||!/^[A-Za-z0-9_-]{40,100}$/.test(descriptor.token))throw new Error('Неверные данные переноса.');
    this.origin='http://127.0.0.1:'+descriptor.port;this.key=transferKey(descriptor.token);this.fetcher=fetcher;
  }
  async request(path,value){
    const key=await this.key;
    const payload=await seal(key,path,value instanceof ArrayBuffer?value:encoder.encode(JSON.stringify(value)));
    let response;
    try{response=await this.fetcher(this.origin+path,{method:'POST',credentials:'omit',targetAddressSpace:'loopback',headers:{'Content-Type':'application/octet-stream','X-AIDJ-IV':payload.iv},body:payload.body,signal:AbortSignal.timeout(90000)});}
    catch{throw new Error('Не удалось связаться с APK на этом телефоне. Открой AI DJ → настройки → «Перенести из Mini App». Если появится запрос локального доступа — разреши его.');}
    let result;
    try{const raw=await unseal(key,'reply:'+path,response.headers.get('X-AIDJ-IV')||'',await response.arrayBuffer());result=JSON.parse(decoder.decode(raw));}
    catch{throw new Error('Не удалось подтвердить связь с APK. Начни перенос заново из AI DJ.');}
    if(!response.ok||result.error)throw new Error(result.error||'Перенос не выполнен.');
    return result;
  }
  async run(tracks,readBlob,{progress=()=>{},completed=()=>{},cancelled=()=>false}={}){
    const manifest=await this.request('/manifest',{tracks});
    const missing=new Set(manifest.missing),existing=new Set(manifest.existing);
    let done=0,copied=0;
    for(const track of tracks){
      if(cancelled())break;
      progress(done,tracks.length,track);
      if(missing.has(track.md5)){
        const content=await readBlob(track.md5);
        if(!content||content.size!==track.bytes)throw new Error('Файл отсутствует в кеше Mini App: '+track.title);
        const reply=await this.request('/file/'+track.md5,await content.arrayBuffer());
        if(!reply.ok||reply.md5!==track.md5)throw new Error('APK не подтвердил сохранение файла.');
        copied++;
      }else if(!existing.has(track.md5))throw new Error('APK вернул неполный список файлов.');
      await completed(track);done++;progress(done,tracks.length,track);
    }
    try{await this.request('/finish',{});}catch{}
    return {done,copied,existing:existing.size};
  }
}

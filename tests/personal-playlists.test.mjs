import test from 'node:test';
import assert from 'node:assert/strict';
import {PersonalPlaylists} from '../miniapp/personal-playlists.mjs';
const a='a'.repeat(24),b='b'.repeat(24),valid=new Set([a,b]);
test('personal playlist edits never accept a shared playlist ID or mutate source arrays',()=>{
  const source=[a],p=new PersonalPlaylists(),created=p.mutate('create',{name:'My playlist',trackIds:source},valid);
  assert.ok(created.id<0);source.push(b);assert.deepEqual(created.trackIds,[a]);
  for(const action of ['add','remove','rename','reorder','delete'])assert.throws(()=>p.mutate(action,{playlistId:1,trackId:a,name:'shared',trackIds:[]},valid));
  p.mutate('add',{playlistId:created.id,trackId:b},valid);p.mutate('add',{playlistId:created.id,trackId:b},valid);
  assert.deepEqual(created.trackIds,[a,b]);p.mutate('reorder',{playlistId:created.id,trackIds:[b,a]},valid);
  assert.deepEqual(created.trackIds,[b,a]);assert.throws(()=>p.mutate('reorder',{playlistId:created.id,trackIds:[b,b]},valid));
  const restored=new PersonalPlaylists(JSON.parse(JSON.stringify(p.items)));restored.mutate('remove',{playlistId:created.id,trackId:a},valid);
  assert.deepEqual(restored.items[0].trackIds,[b]);assert.deepEqual(created.trackIds,[b,a]);
});
test('invalid personal metadata cannot alias shared playlists or exceed bounds',()=>{
  const p=new PersonalPlaylists([{id:1,name:'Volleyball',trackIds:[a]},{id:-1,name:'Own',trackIds:[a,a,'wrong']}]);
  assert.deepEqual(p.items,[{id:-1,name:'Own',personal:true,trackIds:[a]}]);
  assert.throws(()=>p.mutate('create',{name:''},valid));assert.throws(()=>p.mutate('add',{playlistId:-1,trackId:'missing'},valid));
  assert.equal(p.mutate('create',{name:'Second'},valid).id,-2);
});

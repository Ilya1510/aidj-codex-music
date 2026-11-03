import test from 'node:test';
import assert from 'node:assert/strict';
import {rating,selectRated,listSettings} from '../miniapp/ratings.mjs';
const songs=[
 {id:'a',md5:'a',rating:{likes:2,dislikes:0,neutral:1,total:3,mine:0}},
 {id:'b',md5:'b',rating:{likes:5,dislikes:3,neutral:0,total:8,mine:null}},
 {id:'c',md5:'c'},
 {id:'d',md5:'d',rating:{likes:1,dislikes:1,neutral:0,total:2,mine:-1}},
];
test('no-dislikes includes unreviewed songs and can be combined with rated or minimum likes',()=>{
 assert.deepEqual(selectRated(songs,{noDislikes:true}).map(t=>t.id),['a','c']);
 assert.deepEqual(selectRated(songs,{noDislikes:true,state:'rated'}).map(t=>t.id),['a']);
 assert.deepEqual(selectRated(songs,{minLikes:3}).map(t=>t.id),['b']);
 assert.deepEqual(selectRated(songs,{state:'mine-missing'}).map(t=>t.id),['b','c']);
});
test('sorting is stable and never rewrites the stored playlist order',()=>{
 assert.deepEqual(selectRated(songs,{sort:'likes'}).map(t=>t.id),['b','a','d','c']);
 assert.deepEqual(selectRated(songs,{sort:'dislikes'}).map(t=>t.id),['b','d','a','c']);
 assert.deepEqual(songs.map(t=>t.id),['a','b','c','d']);
 assert.deepEqual(selectRated(songs,{state:'unrated'}).map(t=>t.id),['c']);
});
test('offline vote replacement and reset do not count one person twice',()=>{
 assert.deepEqual(rating(songs[0],{value:-1}),{likes:2,dislikes:1,neutral:0,total:3,mine:-1,proposals:0,pending:true});
 assert.equal(rating(songs[0],{value:null}).total,2);
 assert.equal(rating(songs[2],{value:0}).neutral,1);
 assert.equal(selectRated(songs,{state:'mine-missing'},{c:{value:1}}).some(t=>t.id==='c'),false);
});
test('saved list options tolerate older or malformed state',()=>{
 assert.deepEqual(listSettings({sort:'oops',state:'oops',minLikes:-3,noDislikes:'true'}),{sort:'default',state:'all',noDislikes:false,minLikes:0});
});

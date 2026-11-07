import test from 'node:test';
import assert from 'node:assert/strict';
import {rating,selectRated,listSettings,reviewOrder,personVote,priorityRating,movePerson} from '../miniapp/ratings.mjs';
const songs=[
 {id:'a',md5:'a',rating:{likes:2,dislikes:0,neutral:1,total:3,mine:0}},
 {id:'b',md5:'b',rating:{likes:5,dislikes:3,neutral:0,total:8,mine:null}},
 {id:'c',md5:'c'},
 {id:'d',md5:'d',rating:{likes:1,dislikes:1,neutral:0,total:2,mine:-1}},
];
test('no-dislikes includes unreviewed songs and can be combined with rated or minimum net score',()=>{
 assert.deepEqual(selectRated(songs,{noDislikes:true}).map(t=>t.id),['a','c']);
 assert.deepEqual(selectRated(songs,{noDislikes:true,state:'rated'}).map(t=>t.id),['a']);
 assert.deepEqual(selectRated(songs,{minScore:2}).map(t=>t.id),['a','b']);
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
 assert.deepEqual(listSettings({sort:'oops',state:'oops',minLikes:-3,noDislikes:'true'}),{ratingMode:'people',priorityOrder:null,priorityOutcomes:['like'],people:[],peopleMode:'all',sort:'default',state:'all',noDislikes:false,minScore:null});
});

test('net-score threshold supports zero, negatives, clearing and pending votes',()=>{
 const list=[...songs,{id:'e',md5:'e',rating:{likes:0,dislikes:2,total:2}}];
 assert.deepEqual(selectRated(list,{minScore:0}).map(t=>t.id),['a','b','c','d']);
 assert.deepEqual(selectRated(list,{minScore:-2}).map(t=>t.id),['a','b','c','d','e']);
 assert.equal(selectRated(list,{minScore:null}).length,5);
 assert.equal(selectRated(list,{minScore:''}).length,5);
 assert.deepEqual(selectRated(list,{minScore:2},{a:{value:-1}}).map(t=>t.id),['b']);
 assert.equal(listSettings({minLikes:3}).minScore,3);
 assert.equal(listSettings({minLikes:0}).minScore,null);
 assert.equal(listSettings({minScore:'nonsense'}).minScore,null);
});
test('review order gives a modest fixed dislike bonus and never repeats tracks',()=>{
 const list=[songs[0],songs[2],songs[1]];
 assert.deepEqual(reviewOrder(list,{},()=>.5).map(t=>t.id),['b','c','a']);
 assert.equal(new Set(reviewOrder(songs).map(t=>t.id)).size,songs.length);
 assert.deepEqual(list.map(t=>t.id),['a','c','b']);
});

const review={voters:[{id:'self',isMine:true},{id:'masha',label:'Маша'},{id:'sasha',label:'Саша'}],
 votes:{a:{masha:1,sasha:-1},b:{masha:1,sasha:0},c:{sasha:1},d:{masha:-1}}};
const rule=(id,...votes)=>({id,votes});
const ids=(people,extra={},pending={},info=review)=>selectRated(songs,{people,...extra},pending,info).map(t=>t.id);
test('person filters combine people with AND or OR, and outcomes with OR',()=>{
 const both=[rule('masha','like'),rule('sasha','dislike')];
 assert.deepEqual(ids(both),['a']);
 assert.deepEqual(ids(both,{peopleMode:'any'}),['a','b']);
 assert.deepEqual(ids([rule('sasha','neutral','unrated')]),['b','d']);
 assert.deepEqual(ids([rule('masha','unrated')]),['c']);
 assert.deepEqual(ids([rule('masha','like','dislike','neutral')]),['a','b','d']);
 assert.deepEqual(ids([rule('masha','like')],{minScore:2,noDislikes:true}),['a']);
});
test('own filter sees pending offline votes, corrections and resets immediately',()=>{
 assert.deepEqual(ids([rule('self','neutral')]),['a']);
 assert.deepEqual(ids([rule('self','unrated')]),['b','c']);
 assert.deepEqual(ids([rule('self','like')],{},{c:{value:1}}),['c']);
 assert.deepEqual(ids([rule('self','unrated')],{},{a:{value:null}}),['a','b','c']);
 assert.equal(personVote(songs[0],'sasha',review),-1);
});
test('legacy or missing voter data never masquerades as an unrated person',()=>{
 assert.deepEqual(ids([rule('masha','unrated')],{}, {},{voters:review.voters}),[]);
 assert.deepEqual(ids([rule('unknown','unrated')]),[]);
 assert.deepEqual(ids([rule('masha','like')],{}, {},{}),[]);
 assert.deepEqual(ids([rule('masha','unrated')],{}, {},{...review,votes:{}}),['a','b','c','d']);
});
test('people rules survive serialization, reject malformed fields and do not change source order',()=>{
 const value={people:[null,rule('masha','like','garbage','like'),rule('sasha','neutral'),rule('bad id','like'),rule('empty')],peopleMode:'any'};
 const result=listSettings(value);
 assert.deepEqual(result.people,[rule('masha','like'),rule('sasha','neutral')]);
 assert.deepEqual(listSettings(JSON.parse(JSON.stringify(result))),result);
 assert.deepEqual(ids(result.people,{peopleMode:result.peopleMode}),['a','b']);
 assert.deepEqual(songs.map(t=>t.id),['a','b','c','d']);
});

test('priority takes the first decisive vote and skips neutral and absent votes',()=>{
 assert.deepEqual(priorityRating(songs[0],['masha','sasha'],review),{value:1,personId:'masha',known:true});
 assert.deepEqual(priorityRating(songs[0],['sasha','masha'],review),{value:-1,personId:'sasha',known:true});
 assert.deepEqual(priorityRating(songs[1],['sasha','masha'],review),{value:1,personId:'masha',known:true});
 assert.deepEqual(priorityRating(songs[2],['masha','sasha'],review),{value:1,personId:'sasha',known:true});
 assert.deepEqual(priorityRating(songs[2],['self','masha'],review),{value:null,personId:null,known:true});
 assert.deepEqual(priorityRating(songs[0],[],review),{value:null,personId:null,known:true});
});
test('priority defaults to liked songs and replaces standard filters without erasing them',()=>{
 const settings={ratingMode:'priority',priorityOrder:['masha','sasha'],people:[rule('masha','unrated')],noDislikes:true,minScore:100,state:'unrated'};
 assert.deepEqual(selectRated(songs,settings,{},review).map(t=>t.id),['a','b','c']);
 assert.deepEqual(selectRated(songs,{...settings,priorityOrder:['sasha','masha']},{},review).map(t=>t.id),['b','c']);
 assert.deepEqual(selectRated(songs,{...settings,priorityOutcomes:['dislike']},{},review).map(t=>t.id),['d']);
 assert.deepEqual(selectRated(songs,{...settings,priorityOrder:['self'],priorityOutcomes:['unrated']},{},review).map(t=>t.id),['a','b','c']);
 assert.deepEqual(selectRated(songs,{...settings,priorityOutcomes:[]},{},review),[]);
 assert.equal(listSettings(settings).minScore,100);
 assert.deepEqual(selectRated(songs,{...settings,ratingMode:'people'},{},review),[]);
});
test('priority uses local pending corrections and never treats unavailable votes as neutral',()=>{
 assert.equal(priorityRating(songs[0],['self','sasha'],review).value,-1);
 assert.equal(priorityRating(songs[0],['self','sasha'],review,{a:{value:1}}).value,1);
 assert.equal(priorityRating(songs[0],['self','sasha'],review,{a:{value:null}}).value,-1);
 assert.deepEqual(priorityRating(songs[0],['unknown','masha'],review),{value:null,personId:null,known:false});
 assert.deepEqual(priorityRating(songs[0],['masha','sasha'],{voters:review.voters}),{value:null,personId:null,known:false});
 assert.deepEqual(selectRated(songs,{ratingMode:'priority',priorityOrder:['unknown'],priorityOutcomes:['unrated']},{},review),[]);
});
test('priority order survives reopening, removes duplicate IDs and leaves source order intact',()=>{
 const original=['self','masha','sasha'];
 assert.deepEqual(movePerson(original,2,0),['sasha','self','masha']);
 assert.deepEqual(movePerson(original,0,2),['masha','sasha','self']);
 assert.deepEqual(movePerson(original,0,0),original);
 assert.deepEqual(movePerson(original,-1,1),original);
 assert.deepEqual(movePerson(original,1,3),original);
 assert.deepEqual(original,['self','masha','sasha']);
 const value=listSettings({ratingMode:'priority',priorityOrder:['masha','masha',null,'bad id','self'],priorityOutcomes:['dislike','junk']});
 assert.deepEqual(value.priorityOrder,['masha','self']);
 assert.deepEqual(value.priorityOutcomes,['dislike']);
 assert.deepEqual(listSettings(JSON.parse(JSON.stringify(value))),value);
 assert.deepEqual(listSettings({ratingMode:'priority',priorityOrder:[]}).priorityOrder,[]);
});

test('unassessed proposals strictly lead other proposals and ordinary library songs',()=>{
 const list=[
  {id:'library',md5:'l',rating:{dislikes:20,total:20}},
  {id:'assessed',md5:'a',rating:{proposals:1,total:1,neutral:1}},
  {id:'fresh',md5:'f',rating:{proposals:1,total:0}}
 ];
 for(const random of [()=>.00001,()=>.5,()=>.99999])assert.deepEqual(reviewOrder(list,{},random).map(t=>t.id),['fresh','assessed','library']);
 // A pending local neutral vote moves a formerly fresh proposal into the
 // assessed tier without losing its proposal priority over the library.
 const second={id:'second-fresh',md5:'s',rating:{proposals:1,total:0}};
 assert.equal(reviewOrder([...list,second],{f:{value:0}},()=>.5)[0].id,'second-fresh');
});

import assert from 'node:assert/strict';
import test from 'node:test';
import {filterGroups,releaseId,toggleGroupSelection} from './release-selection.js';

const resource=(id,resolution,version)=>({
  resourceHash:id.repeat(40),release:{resolution,version},
});
const groups=[
  {subgroupId:'7',resources:[resource('a','1080p','chs-ja'),resource('b','720p','chs-ja'),resource('c','1080p','cht')]},
  {subgroupId:'8',resources:[resource('d','1080p','chs-ja')]},
];

test('resource filters combine dimensions and allow multiple values in one dimension',()=>{
  const result=filterGroups(groups,{group_ids:['7'],resolutions:['1080p','720p'],versions:['chs-ja']});
  assert.deepEqual(result.map(group=>group.subgroupId),['7']);
  assert.deepEqual(result[0].resources.map(releaseId),['a'.repeat(40),'b'.repeat(40)]);
});

test('subtitle-only and resolution-only filters work independently',()=>{
  assert.equal(filterGroups(groups,{group_ids:[],resolutions:['1080p'],versions:[]})
    .flatMap(group=>group.resources).length,3);
  assert.equal(filterGroups(groups,{group_ids:[],resolutions:[],versions:['cht']})
    .flatMap(group=>group.resources).length,1);
});

test('group select-all touches only its filtered resources and preserves other groups',()=>{
  const visible=filterGroups(groups,{group_ids:[],resolutions:['1080p'],versions:['chs-ja']});
  const selected=new Set(['d'.repeat(40)]);
  const both=toggleGroupSelection(selected,visible[0].resources);
  assert.deepEqual([...both],['d'.repeat(40),'a'.repeat(40)]);
  assert.deepEqual([...toggleGroupSelection(both,visible[0].resources)],['d'.repeat(40)]);
  assert.deepEqual([...selected],['d'.repeat(40)]);
});

test('group select-all deduplicates stable IDs and excludes invalid IDs',()=>{
  const rows=[resource('a','1080p','chs'),resource('a','1080p','chs'),{resourceHash:'invalid'}];
  assert.deepEqual([...toggleGroupSelection(new Set(),rows)],['a'.repeat(40)]);
  assert.equal(releaseId({resourceHash:'A'.repeat(40)}),'a'.repeat(40));
});

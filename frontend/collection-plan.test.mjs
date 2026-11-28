import test from 'node:test';
import assert from 'node:assert/strict';
import {selectionBody,effectiveSelection,episodeRanges} from './collection-plan.js';
test('automatic selection and deliberately empty selection have different requests',()=>{
 assert.deepEqual(selectionBody(null),{});assert.deepEqual(selectionBody(new Set()),{release_ids:[]});
 const plan={items:[{release_id:'one'},{release_id:'two'}]};
 assert.deepEqual([...effectiveSelection(null,plan)],['one','two']);assert.deepEqual([...effectiveSelection(new Set(),plan)],[]);
});
test('episode ranges preserve gaps and count each episode once',()=>{
 assert.equal(episodeRanges([42,1,2,2,3,41,9]),'1–3、9、41–42');assert.equal(episodeRanges([]),'');
});

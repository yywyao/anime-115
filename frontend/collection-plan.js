export function selectionBody(selected){return selected===null?{}:{release_ids:[...selected]};}
export function effectiveSelection(selected,plan){return selected===null?new Set((plan?.items||[]).map(item=>item.release_id)):selected;}
export function episodeRanges(values){
 const numbers=[...new Set(values||[])].sort((a,b)=>a-b),parts=[];
 for(let i=0;i<numbers.length;i++){
  const start=numbers[i];let end=start;
  while(i+1<numbers.length&&numbers[i+1]===end+1)end=numbers[++i];
  parts.push(start===end?String(start):start+'–'+end);
 }
 return parts.join('、');
}

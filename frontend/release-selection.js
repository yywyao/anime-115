export const subtitleVersions={
  chs:'简中',cht:'繁中','chs-ja':'简日','cht-ja':'繁日',
  'chs-cht':'简繁','chs-cht-ja':'简繁日','zh-ja':'中日',
  ja:'日文',none:'无字幕',unknown:'未标注',
};

export const resolutions={
  '1080p':'1080p','2160p':'2160p','720p':'720p','480p':'480p',unknown:'未标注',
};

export function releaseId(resource){
  const value=String(resource?.resourceHash||resource?.hash||'').toLowerCase();
  return /^[a-f0-9]{40}$/.test(value)?value:'';
}

export function matchesRelease(resource,groupId,filters){
  const release=resource.release||{};
  return (!filters.group_ids.length||filters.group_ids.includes(String(groupId)))
    &&(!filters.resolutions.length||filters.resolutions.includes(release.resolution||'unknown'))
    &&(!filters.versions.length||filters.versions.includes(release.version||'unknown'));
}

export function filterGroups(groups,filters){
  return groups.map(group=>({
    ...group,
    resources:(group.resources||[]).filter(resource=>matchesRelease(resource,group.subgroupId,filters)),
  })).filter(group=>group.resources.length);
}

export function toggleGroupSelection(selected,resources){
  const ids=[...new Set(resources.map(releaseId).filter(Boolean))];
  const remove=ids.length>0&&ids.every(id=>selected.has(id));
  const next=new Set(selected);
  for(const id of ids){
    if(remove)next.delete(id);
    else next.add(id);
  }
  return next;
}

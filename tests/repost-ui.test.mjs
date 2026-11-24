import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

test('排序方向保留、全量合计、旧响应不覆盖新选择、刷新不冲掉未保存输入',async()=>{
  const html=fs.readFileSync(new URL('../console/repost.html',import.meta.url),'utf8');
  const elements={};
  const element=id=>elements[id]??={value:'',textContent:'',innerHTML:'',style:{},dataset:{},listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},setAttribute(){}};
  for(const [,id] of html.matchAll(/id="([^"]+)"/g))element(id);
  const columns=['at','view','reply','agree'].map(key=>({...element('th-'+key),dataset:{sort:key}}));
  const arrows=['at','view','reply','agree'].map(key=>({...element('arrow-'+key),dataset:{arrow:key}}));
  const requests=[],stored={rpSort:JSON.stringify({key:'reply',order:'asc'})},intervals=[];
  const context=vm.createContext({...elements,URLSearchParams,AbortController,location:{search:''},
    document:{activeElement:null,visibilityState:'visible',getElementById:element,querySelectorAll:s=>s==='th.sortable'?columns:arrows,addEventListener(){}},
    window:{addEventListener(){}},localStorage:{getItem:k=>stored[k],setItem:(k,v)=>{stored[k]=v;}},
    setInterval:(fn,ms)=>intervals.push({fn,ms}),
    fetch:(url,opt)=>new Promise(resolve=>requests.push({url,opt,resolve}))});
  vm.runInContext(html.split('<script>')[1].split('</script>')[0],context);
  const settle=async(index,title)=>{
    requests[index].resolve({ok:true,json:async()=>({config:{intervalMinutes:30},rows:[{at:'11-24 12:00',title,forum:'a',url:'https://tieba.baidu.com/p/1',tid:1,view:2,ok:true,statsStatus:'fresh'}],
      totals:{view:325,reply:50,agree:20,averageView:13,covered:25,fresh:1},page:1,pages:3,total:25})});
    for(let i=0;i<8;i++)await Promise.resolve();
  };
  assert.match(requests[0].url,/sort=reply&order=asc/);
  await settle(0,'最初');assert.equal(elements.sumView.textContent,'325');
  elements.interval.value='99';elements.interval.listeners.input();
  vm.runInContext("setSort('view');setOrder('desc')",context);
  assert.equal(requests.length,3);
  await settle(2,'新结果');await settle(1,'过时结果');
  assert.match(elements.rows.innerHTML,/新结果/);assert.doesNotMatch(elements.rows.innerHTML,/过时结果/);
  assert.equal(elements.interval.value,'99');
  assert.deepEqual(JSON.parse(stored.rpSort),{key:'view',order:'desc'});
  assert.equal(intervals[0].ms,5000);
});

'use strict';
// Chart data is fictional and comes from the SAME backend simulation as trade prices.
// Company names and ticker symbols are separate reference information, NOT a price feed.
const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
const logged = document.body.dataset.logged === '1';
const state = {
  symbol: 'DEMO-A', name: 'Example Industries', exchange: 'DEMO',
  candles: [], interval: 5, pro: ['LOCAL_PRO','PRO_TEST'].includes(document.body.dataset.tier),
  chartMode: 'fictional', researchCandles: [], researchDay: '',
  cash: 100000, positions: {}, marks: {}, history: [], watchlist: []
};
const fmt = n => '₹' + Number(n).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2});
const el = id => document.getElementById(id);
let visibleSeries = [], searchOffset=0, searchTimeout=null, selectedRequest=0;
const timeLabel = n => `${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;
async function requestJSON(url, opts={}) {
  const r = await fetch(url, {credentials:'same-origin',cache:'no-store',...opts});
  let result;
  try { result = await r.json(); } catch (_) { throw new Error('The server did not return JSON.'); }
  if(!r.ok) throw new Error(result.error || `Request failed (${r.status})`);
  return result;
}
function api(url,data={}){return requestJSON(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});}
function status(msg){el('trade-status').textContent=msg;}
function setPro(){el('pricing').scrollIntoView({behavior:'smooth'});status('Only server-verified TEST Pro can place virtual orders.');}
async function loadMarket(symbol, selection={}) {
  const turn=++selectedRequest;
  status('Loading simulated exercise chart...');
  try {
    const info=await requestJSON('/api/market/'+encodeURIComponent(symbol));
    if(turn!==selectedRequest)return;
    state.symbol=info.symbol;state.name=info.name;state.exchange=info.exchange;
    state.candles=info.candles;state.researchCandles=[];
    // Server quote is derived from this exact same chart.
    state.practicePrice=info.practice_price;
    if(!state.watchlist.some(x=>x.symbol===info.symbol))state.watchlist.unshift({symbol:info.symbol,name:info.name,exchange:info.exchange});
    state.watchlist=state.watchlist.slice(0,10);
    setupStocks();
    if(state.chartMode==='research'){
      try{await loadResearch();}catch(e){status(e.message);state.chartMode='fictional';state.researchCandles=[];
        el('research-mode')?.classList.remove('active');el('simulation-mode')?.classList.add('active');}
    }
    updateAll();if(state.chartMode==='fictional')status('Fictional exercise loaded. Prices are not market quotes.');
  }catch(e){if(turn===selectedRequest)status(e.message);}
}
function setupStocks(){
  const container=el('symbol-list');container.replaceChildren();
  state.watchlist.forEach(inst=>{
    const b=document.createElement('button');b.type='button';
    b.className='symbol'+(inst.symbol===state.symbol?' selected':'');
    const title=document.createElement('span'),sym=document.createElement('strong'),name=document.createElement('small');
    sym.textContent=inst.symbol;name.textContent=inst.name;title.append(sym,name);
    const exchange=document.createElement('span');exchange.className='symbol-price';exchange.textContent=inst.exchange;
    b.append(title,exchange);b.addEventListener('click',()=>loadMarket(inst.symbol));container.append(b);
  });
}
async function loadResearch(){
  if(state.exchange==='DEMO'){state.researchCandles=[];throw new Error('Choose a real NSE/BSE company from search for local historical research.');}
  const r=await requestJSON('/api/research/candles?symbol='+encodeURIComponent(state.symbol)+'&interval='+state.interval+'m');
  state.researchCandles=r.candles;state.researchDay=r.day;
}
function aggregate(arr,interval){
  let out=[];for(let i=0;i<arr.length;i+=interval){let slice=arr.slice(i,i+interval);
    out.push({minutes:slice[0].minutes,open:slice[0].open,close:slice.at(-1).close,
      high:Math.max(...slice.map(x=>x.high)),low:Math.min(...slice.map(x=>x.low))});}
  return out;
}
function linePath(vals,width,height,padding=5){
  if(vals.length<2)return '';
  const low=Math.min(...vals),high=Math.max(...vals),range=Math.max(high-low,1);
  return vals.map((y,i)=>`${i?'L':'M'} ${padding+i/(vals.length-1)*(width-padding*2)} ${padding+(1-(y-low)/range)*(height-padding*2)}`).join(' ');
}
function updateHeader(){
  if(!state.candles.length)return;
  const historical=state.chartMode==='research'&&state.researchCandles.length;
  const candles=historical?state.researchCandles:state.candles,last=historical?candles.at(-1).close:state.practicePrice,first=candles[0].open,pct=(last/first-1)*100;
  el('symbol-name').textContent=state.name;
  el('symbol-code').textContent=state.symbol+' · '+state.exchange+' · '+(historical?'HISTORICAL RESEARCH; MAY BE DELAYED':'FICTIONAL PRACTICE PRICE');
  el('last-price').textContent=fmt(last);
  el('sample-change').textContent=(pct>=0?'+':'')+pct.toFixed(2)+'% '+(historical?'historical':'fictional');
  el('sample-change').style.color=pct>=0?'#2de4aa':'#ff6e81';
  el('hero-price').textContent=fmt(last);
  let hero=candles.filter((_,i)=>i%5===0).map(x=>x.close);
  const path=linePath(hero,350,100,4);
  // Path coordinates are from server-provided numeric data only.
  el('hero-spark').innerHTML=`<svg viewBox="0 0 350 110" preserveAspectRatio="none"><path d="${path}" stroke="#31dfac" fill="none" stroke-width="2.3"/></svg>`;
}
function drawChart(){
  if(!visibleSeries.length)return;
  const canvas=el('chart'),rect=canvas.parentElement.getBoundingClientRect(),w=Math.max(260,rect.width),h=Math.max(205,rect.height);
  const dpr=Math.max(1,window.devicePixelRatio||1);canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
  const left=9,right=78,top=14,bottom=31,plotW=w-left-right,plotH=h-top-bottom;
  const lows=visibleSeries.map(d=>d.low),highs=visibleSeries.map(d=>d.high);
  const mn=Math.min(...lows),mx=Math.max(...highs),pad=(mx-mn)*.13||5,lo=mn-pad,hi=mx+pad,y=v=>top+(1-(v-lo)/(hi-lo))*plotH;
  ctx.font='11px system-ui';ctx.textBaseline='middle';ctx.strokeStyle='#294057';ctx.lineWidth=1;
  for(let i=0;i<5;i++){let v=lo+(hi-lo)*i/4,yy=y(v);ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(left+plotW,yy);ctx.stroke();ctx.fillStyle='#91a9c0';ctx.fillText('₹'+v.toFixed(1),left+plotW+8,yy);}
  const stride=plotW/visibleSeries.length,body=Math.min(13,Math.max(1,stride*.64));
  visibleSeries.forEach((d,i)=>{let x=left+stride*(i+.5),up=d.close>=d.open;ctx.strokeStyle=up?'#37dfac':'#ef6f85';ctx.fillStyle=ctx.strokeStyle;
    ctx.beginPath();ctx.moveTo(x,y(d.low));ctx.lineTo(x,y(d.high));ctx.stroke();
    let yt=y(Math.max(d.open,d.close)),yb=y(Math.min(d.open,d.close));ctx.fillRect(x-body/2,yt,body,Math.max(1.2,yb-yt));});
  ctx.fillStyle='#88a3ba';ctx.textBaseline='top';
  for(let i=0;i<5;i++){const index=Math.floor(i*(visibleSeries.length-1)/4),x=left+stride*(index+.5);
    ctx.fillText(timeLabel(visibleSeries[index].minutes),Math.min(w-130,Math.max(6,x-14)),h-bottom+8);}
  if(state.pro&&visibleSeries.length>6){
    const closes=visibleSeries.map(v=>v.close);ctx.strokeStyle='#73a5fa';ctx.lineWidth=1.6;ctx.beginPath();
    let started=false;closes.forEach((v,i)=>{if(i<Math.min(19,closes.length-1))return;
      const avg=closes.slice(Math.max(0,i-19),i+1).reduce((a,b)=>a+b,0)/Math.min(i+1,20),x=left+stride*(i+.5),yy=y(avg);
      if(!started){ctx.moveTo(x,yy);started=true;}else ctx.lineTo(x,yy);});ctx.stroke();}
  canvas._chartGeom={left,stride};
}
function updateTrading(){
  const access=el('access-label');access.textContent=state.pro?'PRO TEST · VIRTUAL ORDERS':'FREE · VIEW ONLY';access.style.color=state.pro?'#2de4aa':'';
  el('access-message').textContent=!logged?'Sign in to save progress. Virtual trading is locked.':state.pro?'Virtual trading enabled; no real securities or currency are exchanged.':'Free members can view charts; only verified test Pro can trade.';
  el('free-button').classList.toggle('toggle-on',!state.pro);el('pro-button').classList.toggle('toggle-on',state.pro);
  el('buy-button').disabled=!state.pro||!logged||!state.candles.length||state.chartMode==='research';
  el('sell-button').disabled=!state.pro||!logged||!state.candles.length||state.chartMode==='research';
  el('reset-button').disabled=!state.pro||!logged;
  el('indicator-line').textContent=state.pro?'Pro indicator: 20-bar moving average on FICTIONAL data.':'Moving average available with test Pro.';
}
function updatePortfolio(){
  el('wallet').textContent=fmt(state.cash);let markup='',invested=0;el('positions').replaceChildren();
  for(const [symbol,qty] of Object.entries(state.positions)){
    if(!qty)continue;const current=(state.marks[symbol] ?? (symbol===state.symbol?state.practicePrice:0));const mark=(current||0)*qty;invested+=mark;
    const row=document.createElement('div');row.className='position';
    const label=document.createElement('span');label.textContent=symbol+' × '+qty;
    const value=document.createElement('strong');value.textContent=fmt(mark);row.append(label,value);
    el('positions').append(row);markup='filled';
  }
  if(!markup)el('positions').textContent='No simulated trades yet.';
  // Server values are authoritative for saved portfolios; these display calculations are illustrative.
  el('invested').textContent=fmt(invested);el('equity').textContent=fmt(invested+state.cash);
  el('history').replaceChildren();
  if(!state.history.length){el('history').textContent='No saved fictional trades yet.';return;}
  for(const t of state.history){let row=document.createElement('div');row.className='history-row';let label=document.createElement('span');label.textContent=`${t.side} ${t.qty} × ${t.symbol}`;
    let value=document.createElement('span');value.textContent=fmt(t.price*t.qty);row.append(label,value);el('history').append(row);}
}
function updateAll(){if(!state.candles.length)return;visibleSeries=state.chartMode==='research'?state.researchCandles:aggregate(state.candles,state.interval);updateHeader();updateTrading();updatePortfolio();drawChart();}
function applyPortfolio(p){if(!p)return;state.cash=p.cash;state.positions=p.positions;state.marks=p.marks||{};state.history=p.history;state.pro=['LOCAL_PRO','PRO_TEST'].includes(p.tier);updateAll();}
async function loadPortfolio(){if(!logged)return;try{applyPortfolio(await requestJSON('/api/portfolio'));}catch(e){status(e.message);}}
async function order(side){
  if(!logged){location.href='/register';return;}
  if(!state.pro){setPro();return;}
  if(state.chartMode==='research'){status('Switch back to fictional charts before placing virtual orders.');return;}
  const qty=Number(el('quantity').value);if(!Number.isSafeInteger(qty)||qty<1||qty>10000){status('Enter quantity between 1 and 10,000.');return;}
  try{const r=await api('/api/order',{symbol:state.symbol,side,quantity:qty});applyPortfolio(r.portfolio);status(r.message);}
  catch(e){status(e.message);}
}
async function searchStocks(reset=true){
  if(reset)searchOffset=0;
  const q=el('stock-search').value.trim(),exchange=el('stock-exchange').value,box=el('search-results');
  el('directory-status').textContent='Searching available Indian stock listings...';
  try{
    const r=await requestJSON('/api/stocks?q='+encodeURIComponent(q)+'&exchange='+exchange+'&offset='+searchOffset+'&limit=20');
    box.replaceChildren();
    r.stocks.forEach(inst=>{
      let b=document.createElement('button');b.type='button';b.className='search-stock';
      let name=document.createElement('span');name.textContent=inst.name;
      let code=document.createElement('small');code.textContent=inst.symbol+' · '+inst.exchange;
      b.append(name,code);b.onclick=()=>loadMarket(inst.symbol);box.append(b);
    });
    if(!r.stocks.length)box.textContent='No matches in the available directory.';
    el('directory-status').textContent=`${r.total} available results · ${r.partial?'Partial directory; some exchange feeds unavailable.':'Loaded directory.'} Prices are simulated.`;
    el('search-prev').disabled=searchOffset===0;el('search-next').disabled=searchOffset+20>=r.total;
  }catch(e){el('directory-status').textContent=e.message;}
}
function hover(event){const g=el('chart')._chartGeom;if(!g)return;let rect=el('chart').getBoundingClientRect(),x=event.clientX-rect.left,i=Math.floor((x-g.left)/g.stride);
  const tip=el('chart-tip');if(i<0||i>=visibleSeries.length){tip.hidden=true;return;}let d=visibleSeries[i];tip.hidden=false;
  tip.textContent=`${timeLabel(d.minutes)} IST · ${state.chartMode==='research'?'HISTORICAL; MAY BE DELAYED':'FICTIONAL'} · O ${d.open.toFixed(2)} H ${d.high.toFixed(2)} L ${d.low.toFixed(2)} C ${d.close.toFixed(2)}`;
  tip.style.left=Math.min(Math.max(3,x-55),rect.width-180)+'px';tip.style.top='7px';}
function init(){
  state.watchlist=[{symbol:'DEMO-A',name:'Example Industries',exchange:'DEMO'}];
  loadMarket('DEMO-A');loadPortfolio();searchStocks();
  el('stock-search').addEventListener('input',()=>{clearTimeout(searchTimeout);searchTimeout=setTimeout(()=>searchStocks(),250);});
  el('stock-exchange').addEventListener('change',()=>searchStocks());
  el('search-next').onclick=()=>{searchOffset+=20;searchStocks(false);};
  el('search-prev').onclick=()=>{searchOffset=Math.max(0,searchOffset-20);searchStocks(false);};
  document.querySelectorAll('.interval').forEach(b=>b.onclick=async()=>{
    state.interval=Number(b.dataset.interval);document.querySelectorAll('.interval').forEach(x=>x.classList.toggle('active',x===b));
    if(state.chartMode==='research'){try{await loadResearch();}catch(e){status(e.message);state.researchCandles=[];return;}}
    updateAll();});
  const historical=el('research-mode'),virtual=el('simulation-mode');
  if(historical&&virtual){
    historical.onclick=async()=>{try{await loadResearch();state.chartMode='research';historical.classList.add('active');virtual.classList.remove('active');updateAll();status('Historical research only. Virtual orders disabled while viewing historical data.');}
       catch(e){status(e.message);state.chartMode='fictional';updateAll();}};
    virtual.onclick=()=>{state.chartMode='fictional';virtual.classList.add('active');historical.classList.remove('active');updateAll();status('Fictional exercise mode.');};
  }
  el('free-button').onclick=()=>status(state.pro?'Your TEST Pro stays active.':'Free plan has read-only chart access.');
  el('pro-button').onclick=setPro;el('buy-button').onclick=()=>order('BUY');el('sell-button').onclick=()=>order('SELL');
  el('reset-button').onclick=async()=>{if(!state.pro)return;try{applyPortfolio((await api('/api/reset')).portfolio);status('Virtual portfolio reset.');}catch(e){status(e.message);}};
  const local=el('local-pro-button');if(local)local.onclick=async()=>{try{await api('/api/dev/pro');location.reload();}catch(e){alert(e.message);}};
  const checkout=el('test-checkout-button');if(checkout)checkout.onclick=async()=>{
    const member=el('membership-status');try{
      if(!el('autopay-consent').checked)throw new Error('Read and accept the TEST recurring mandate terms.');
      if(typeof Razorpay==='undefined')throw new Error('Razorpay checkout is unavailable.');
      const r=await api('/api/test/subscribe');
      const widget=new Razorpay({key:r.key_id,subscription_id:r.subscription_id,name:'TradeX AI TEST',description:'UPI AutoPay TEST · ₹999 monthly · 12 cycles',
        config:{display:{blocks:{upi:{name:'UPI AutoPay TEST',instruments:[{method:'upi'}]}},sequence:['block.upi'],preferences:{show_default_blocks:false}}},
        handler:async response=>{try{const result=await api('/api/test/verify',response);member.textContent=result.message;}catch(e){member.textContent=e.message;}}});
      widget.on('payment.failed',()=>member.textContent='Test authorization failed or UPI AutoPay unavailable for merchant.');widget.open();
    }catch(e){member.textContent=e.message;}
  };
  const refresh=el('subscription-status-button');if(refresh)refresh.onclick=async()=>{const box=el('membership-status');box.textContent='Checking Razorpay TEST subscription...';
    try{const s=await requestJSON('/api/test/subscription-status');if(!s.enabled){box.textContent=s.message;return;}
      if(s.state==='none'){box.textContent='No TEST mandate started.';return;}
      const when=s.current_period_end?new Date(s.current_period_end*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'}):'not yet confirmed';
      box.textContent=`TEST status: ${s.state} · Verified method: ${s.payment_method} · Paid cycles: ${s.paid_cycles} · Period ends: ${when} IST. ${s.pro?'Pro active — refresh page.':'Pro locked until verified active UPI period.'}`;
    }catch(e){box.textContent=e.message;}};
  const cancel=el('test-cancel-button');if(cancel)cancel.onclick=async()=>{if(!confirm('Cancel your Razorpay TEST mandate?'))return;
    try{el('membership-status').textContent=(await api('/api/test/cancel')).message;}catch(e){el('membership-status').textContent=e.message;}};
  el('chart').addEventListener('mousemove',hover);el('chart').addEventListener('mouseleave',()=>el('chart-tip').hidden=true);
  window.addEventListener('resize',drawChart);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();

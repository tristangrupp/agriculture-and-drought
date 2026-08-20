"""Render the self-contained drought dashboard.

Produces `output/brazil_drought_dashboard.html`: a Brazil map of HydroBASINS level-6
basins with a 2019-2025 year slider and a metric switch, and a per-basin panel that
draws the daily 30-day accumulation against its day-of-year threshold with the detected
drought events marked underneath.

Everything is embedded: geometry, summary tables, event tables and the daily traces are
gzipped, base64'd and inflated in the browser, so the file works offline with no server.
Traces are sub-sampled every TRACE_STEP days - P30 is a 30-day rolling total, so nothing
visible is lost - while the event bars keep full daily precision.

Run in the `ftw` env (matplotlib/geopandas there; not needed for plotting, but this env's
GDAL handles the simplify + GeoJSON export without the crashes seen in `crop`):
    <ftw-python> dashboard.py
"""
import base64
import gzip
import json
import os

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg

TRACE_STEP = 5
SIMPLIFY_DEG = 0.03
OUT_HTML = os.path.join(cfg.OUT_DIR, "brazil_drought_dashboard.html")

METRICS = [
    ("total_drought_days", "Drought days"),
    ("longest_event_days", "Longest event (days)"),
    ("n_events", "Number of events"),
    ("total_deficit_mm", "Rainfall deficit (mm)"),
]


def pack(obj):
    raw = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
    return base64.b64encode(gzip.compress(raw, 6)).decode()


def main():
    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    summ = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv"))
    events = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"))
    st = np.load(os.path.join(cfg.DATA_DIR, "detection_state.npz"))
    thr = np.load(cfg.THRESHOLDS)

    geo = basins[["bidx", "HYBAS_ID", "SUB_AREA", "frac_br", "geometry"]].copy()
    geo["geometry"] = geo.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)
    geo = geo[~geo.geometry.is_empty]
    gj = json.loads(geo.to_json(drop_id=True))
    for f in gj["features"]:
        f["properties"]["bidx"] = int(f["properties"]["bidx"])

    dates = st["dates"]
    step = slice(None, None, TRACE_STEP)
    tdates = dates[step].tolist()
    p30 = np.nan_to_num(st["p30"][step], nan=-1).astype("float32")

    # threshold expanded onto the same sampled dates
    d0 = [pd.Timestamp(str(d)) for d in tdates]
    pdoy = []
    for d in d0:
        doy = d.dayofyear
        leap = d.is_leap_year
        pdoy.append(doy - 1 if (leap and doy >= 60) else doy)
    # -1 is the sentinel for "no data" on both traces; JSON has no NaN literal, and one
    # coastal basin has no valid CHIRPS pixels at all.
    thr_t = np.nan_to_num(thr["thr20"][np.array(pdoy) - 1], nan=-1).astype("float32")
    ass_t = thr["assessable"][np.array(pdoy) - 1]

    payload = {
        "dates": tdates,
        "years": list(cfg.EVENT_YEARS),
        "metrics": METRICS,
        "summary": {str(y): {m: summ.loc[summ.year == y].set_index("bidx")[m]
                             .reindex(range(len(basins))).fillna(0).round(1).tolist()
                             for m, _ in METRICS} for y in cfg.EVENT_YEARS},
        # events carry NaN in mean_intensity where a basin has no valid pixels
        "events": events[["bidx", "start", "end", "duration_days", "deficit_mm",
                          "mean_intensity", "severe_days", "wet_days_absorbed",
                          "start_year"]].fillna(-1).to_dict("records"),
        "p30": [np.round(p30[:, b], 1).tolist() for b in range(p30.shape[1])],
        "thr": [np.round(thr_t[:, b], 1).tolist() for b in range(thr_t.shape[1])],
        "assessable": [ass_t[:, b].astype("uint8").tolist() for b in range(ass_t.shape[1])],
        "hybas": basins["HYBAS_ID"].astype("int64").astype(str).tolist(),
        "area": basins["SUB_AREA"].round(0).tolist(),
    }

    html = TEMPLATE.replace("__GEO__", pack(gj)).replace("__DATA__", pack(payload))

    # The field-level demo section is optional: it only appears once demo_fields.py,
    # demo_ndvi.py and demo_index.py have produced their outputs.
    try:
        import demo_page
        demo_payload = demo_page.build_payload()
    except Exception as e:                       # noqa: BLE001
        print("demo section skipped: %s" % e)
        demo_payload = []
    if demo_payload:
        html = (html.replace("/*__DEMO_CSS__*/", demo_page.SECTION_CSS)
                    .replace("<!--__DEMO_HTML__-->", demo_page.SECTION_HTML)
                    .replace("/*__DEMO_JS__*/",
                             demo_page.SECTION_JS.replace("__DEMO__", pack(demo_payload))))
        print("demo section: %d cases embedded" % len(demo_payload))

    with open(OUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("wrote %s (%.1f MB)" % (OUT_HTML, os.path.getsize(OUT_HTML) / 1e6))


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brazil Drought 2019-2025</title>
<style>
:root{--bg:#f7f9fb;--panel:#ffffff;--ink:#182029;--dim:#5f6b7a;--line:#dde3ea;--accent:#1668c4;
--nodata:#dfe5ec;--grid:#e8edf3;--thr:#9aa5b3;--shade:#c0392b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line)}
h1{margin:0 0 4px;font-size:19px;font-weight:600}
.sub{color:var(--dim);font-size:12.5px}
.wrap{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:14px;padding:14px 20px 28px}
@media(max-width:1000px){.wrap{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;
box-shadow:0 1px 2px rgba(16,24,40,.04)}
.ctl{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-bottom:10px}
select,input[type=range]{accent-color:var(--accent)}
select{background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
.yr{font-variant-numeric:tabular-nums;font-weight:600;min-width:44px}
svg{width:100%;height:auto;display:block}
path.basin{stroke:#ffffff;stroke-width:.3;cursor:pointer}
path.basin:hover{stroke:#182029;stroke-width:1}
path.basin.sel{stroke:var(--accent);stroke-width:1.4}
.legend{display:flex;gap:2px;margin-top:8px;align-items:center}
.legend i{display:block;height:11px;flex:1}
.lbl{display:flex;justify-content:space-between;color:var(--dim);font-size:11.5px;margin-top:3px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:8px}
th,td{text-align:right;padding:4px 6px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--dim);font-weight:500}
.tip{position:fixed;pointer-events:none;background:#ffffff;border:1px solid var(--line);
border-radius:6px;padding:6px 9px;font-size:12px;opacity:0;transition:opacity .1s;
box-shadow:0 4px 14px rgba(16,24,40,.14);color:var(--ink)}
.k{color:var(--dim)}
.note{color:var(--dim);font-size:12px;margin-top:10px}
/*__DEMO_CSS__*/
</style></head><body>
<header>
<h1>Anomalous drought in Brazil, 2019-2025</h1>
<div class="sub">CHIRPS v2.0 daily rainfall, HydroBASINS level 6. A basin-day counts as drought when its
30-day rainfall total falls below the 20th percentile that the 1990-2010 baseline produced for that
same day of the year, so a normal dry season is not counted.</div>
</header>
<div class="wrap">
  <div class="card">
    <div class="ctl">
      <label>Year <input type="range" id="yr" min="0" max="6" value="0"></label>
      <span class="yr" id="yrlab"></span>
      <label>Metric <select id="metric"></select></label>
    </div>
    <svg id="map" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet"></svg>
    <div class="legend" id="leg"></div>
    <div class="lbl"><span id="lmin"></span><span id="lmax"></span></div>
    <div class="note">Click a basin to see its daily trace. Grey = no drought that year.</div>
  </div>
  <div class="card">
    <div id="btitle" style="font-weight:600;margin-bottom:2px">Select a basin</div>
    <div class="sub" id="bsub">The panel shows 30-day accumulated rainfall against the
    day-of-year drought threshold, with detected events underneath.</div>
    <svg id="trace" viewBox="0 0 820 352"></svg>
    <table id="evt"></table>
  </div>
</div>
<!--__DEMO_HTML__-->
<div class="tip" id="tip"></div>
<script>
const RAMP=["#fff3d9","#fed9a6","#fdb96f","#f99244","#ec6626","#c9401a","#8f1d12"];
async function unpack(b64){
  const bin=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
  const ds=new DecompressionStream("gzip");
  const txt=await new Response(new Blob([bin]).stream().pipeThrough(ds)).text();
  return JSON.parse(txt);
}
(async()=>{
const GEO=await unpack("__GEO__"), D=await unpack("__DATA__");
const yrEl=document.getElementById("yr"), mEl=document.getElementById("metric");
yrEl.max=D.years.length-1;
D.metrics.forEach(([k,l],i)=>{const o=document.createElement("option");o.value=k;o.textContent=l;mEl.appendChild(o)});

// ---- projection ------------------------------------------------------------
let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
const rings=[];
for(const f of GEO.features){
  const g=f.geometry, polys = g.type==="Polygon"?[g.coordinates]:g.coordinates;
  const rr=[];
  for(const poly of polys) for(const ring of poly){
    rr.push(ring);
    for(const [x,y] of ring){if(x<x0)x0=x;if(x>x1)x1=x;if(y<y0)y0=y;if(y>y1)y1=y}
  }
  rings.push({bidx:f.properties.bidx,rings:rr});
}
const W=1000,H=Math.round(W*(y1-y0)/(x1-x0)/Math.cos((y0+y1)/2*Math.PI/180));
const sx=v=>(v-x0)/(x1-x0)*W, sy=v=>(y1-v)/(y1-y0)*H;
const map=document.getElementById("map");
map.setAttribute("viewBox",`0 0 ${W} ${H}`);
const paths=[];
for(const r of rings){
  let d="";
  for(const ring of r.rings){
    d+="M"+ring.map(([x,y])=>sx(x).toFixed(1)+","+sy(y).toFixed(1)).join("L")+"Z";
  }
  const p=document.createElementNS("http://www.w3.org/2000/svg","path");
  p.setAttribute("d",d); p.setAttribute("class","basin"); p.dataset.b=r.bidx;
  map.appendChild(p); paths.push(p);
}

// ---- choropleth ------------------------------------------------------------
let sel=null;
function draw(){
  const y=D.years[+yrEl.value], m=mEl.value, v=D.summary[y][m];
  document.getElementById("yrlab").textContent=y;
  const pos=v.filter(a=>a>0).sort((a,b)=>a-b);
  const hi=pos.length?pos[Math.floor(pos.length*0.97)]||pos[pos.length-1]:1;
  for(const p of paths){
    const val=v[p.dataset.b];
    p.setAttribute("fill", val>0 ? RAMP[Math.min(RAMP.length-1,
      Math.max(0,Math.floor(val/hi*(RAMP.length-1))))] : "#dfe5ec");
    p._v=val;
  }
  const leg=document.getElementById("leg"); leg.innerHTML="";
  RAMP.forEach(c=>{const i=document.createElement("i");i.style.background=c;leg.appendChild(i)});
  document.getElementById("lmin").textContent="0";
  document.getElementById("lmax").textContent=hi.toFixed(hi<10?1:0)+"+";
}
yrEl.oninput=draw; mEl.onchange=draw;

// ---- tooltip + selection ---------------------------------------------------
const tip=document.getElementById("tip");
map.addEventListener("mousemove",e=>{
  const t=e.target.closest("path.basin");
  if(!t){tip.style.opacity=0;return}
  const b=t.dataset.b;
  tip.innerHTML=`<b>HYBAS ${D.hybas[b]}</b><br><span class="k">${D.area[b].toLocaleString()} km&sup2;</span><br>`+
    `${mEl.selectedOptions[0].textContent}: <b>${t._v}</b>`;
  tip.style.left=(e.clientX+14)+"px"; tip.style.top=(e.clientY+14)+"px"; tip.style.opacity=1;
});
map.addEventListener("mouseleave",()=>tip.style.opacity=0);
map.addEventListener("click",e=>{
  const t=e.target.closest("path.basin"); if(!t)return;
  paths.forEach(p=>p.classList.remove("sel")); t.classList.add("sel");
  sel=+t.dataset.b; trace();
});

// ---- per-basin trace -------------------------------------------------------
function ymd(n){const s=""+n;return s.slice(0,4)+"-"+s.slice(4,6)+"-"+s.slice(6)}
function trace(){
  const sv=document.getElementById("trace"); sv.innerHTML="";
  if(sel===null)return;
  const y=D.years[+yrEl.value];
  const idx=[]; D.dates.forEach((d,i)=>{if(Math.floor(d/10000)===y)idx.push(i)});
  const p30=idx.map(i=>D.p30[sel][i]), th=idx.map(i=>D.thr[sel][i]);
  const as=idx.map(i=>D.assessable[sel][i]);
  const W=820,H=352,L=46,R=10,T=18,B=104;
  const mx=Math.max(1,...p30,...th);
  const px=i=>L+i/(idx.length-1)*(W-L-R), py=v=>T+(1-v/mx)*(H-T-B);
  const ns="http://www.w3.org/2000/svg";
  const add=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);sv.appendChild(e);return e};
  for(let g=0;g<=4;g++){const v=mx*g/4;
    add("line",{x1:L,x2:W-R,y1:py(v),y2:py(v),stroke:"#e8edf3"});
    const t=add("text",{x:L-6,y:py(v)+4,fill:"#5f6b7a","font-size":11,"text-anchor":"end"});t.textContent=Math.round(v)}
  const line=(arr,col,w)=>add("path",{d:"M"+arr.map((v,i)=>px(i).toFixed(1)+","+py(Math.max(v,0)).toFixed(1)).join("L"),
    fill:"none",stroke:col,"stroke-width":w});
  // shade drought
  for(let i=0;i<idx.length;i++){
    if(as[i]&&p30[i]>=0&&p30[i]<th[i])
      add("rect",{x:px(i)-1.5,y:T,width:3,height:H-T-B,fill:"#c0392b","fill-opacity":.13});
  }
  line(th,"#9aa5b3",1.4); line(p30,"#1668c4",1.8);
  // month ticks
  idx.forEach((i,k)=>{const d=D.dates[i]; if(d%100<=5&&(Math.floor(d/100)%100)%2===1){
    const t=add("text",{x:px(k),y:H-B+15,fill:"#5f6b7a","font-size":10,"text-anchor":"middle"});
    t.textContent=ymd(d).slice(5,7)}});
  // event bars
  const ev=D.events.filter(e=>e.bidx===sel&&(e.start.slice(0,4)==y||e.end.slice(0,4)==y));
  const t0=ymd(D.dates[idx[0]]), t1=ymd(D.dates[idx[idx.length-1]]);
  const frac=s=>{const a=new Date(t0),b=new Date(t1),c=new Date(s);
    return Math.min(1,Math.max(0,(c-a)/(b-a)))};
  ev.forEach((e,k)=>{
    const xa=L+frac(e.start)*(W-L-R), xb=L+frac(e.end)*(W-L-R);
    add("rect",{x:xa,y:H-B+44+k*12,width:Math.max(2,xb-xa),height:8,rx:2,fill:"#ec6626"});
  });
  add("text",{x:L,y:H-B+37,fill:"#5f6b7a","font-size":11}).textContent =
    ev.length? "detected events" : "no drought events this year";
  document.getElementById("btitle").textContent="HYBAS "+D.hybas[sel];
  document.getElementById("bsub").textContent =
    D.area[sel].toLocaleString()+" km² · blue = 30-day rainfall, grey = threshold, red shading = in drought";
  const tb=document.getElementById("evt");
  tb.innerHTML="<tr><th>start</th><th>end</th><th>days</th><th>deficit mm</th><th>wet days absorbed</th></tr>"+
    ev.map(e=>`<tr><td>${e.start}</td><td>${e.end}</td><td>${e.duration_days}</td>`+
      `<td>${e.deficit_mm.toFixed(0)}</td><td>${e.wet_days_absorbed}</td></tr>`).join("");
}
yrEl.addEventListener("input",trace);
draw();
})();
/*__DEMO_JS__*/
</script></body></html>"""


if __name__ == "__main__":
    main()

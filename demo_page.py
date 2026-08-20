"""Build the field-level demo section that gets embedded in the dashboard.

Exports `build_payload()` plus the HTML/CSS/JS fragments that `dashboard.py` injects, so
the four buttons live on the same page as the national map rather than in a separate file.

Each demo carries:
    fields      simplified polygon rings in AOI-local integer coordinates
    anom        (ntime, nfield) int8, NDVI minus the scene median, x100
    drought     the basin's daily P30 and threshold across the imagery window
    events      the drought events overlapping the window

Geometry is quantised to a 0-4000 integer grid inside the AOI and rings are simplified,
because four AOIs hold ~14,500 polygons between them and shipping full float coordinates
would dominate the page size.

Colours: tagged rice and sugarcane get their own outline, every other field is a thin
black outline, and the fill is an orange ramp driven by whichever water signal is stronger
on that date - greenness held above the neighbours, or open water inside the field. Rice
needs the second one: a flooded paddy draws the most water in the scene and reads as the
LOWEST NDVI, so a greenness-only fill would leave it blank.
"""
import json
import os

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg
import demo_config as dc

SIMPLIFY_M = 25.0        # metres; fields are >= 1 ha so this keeps their shape
GRID = 4000              # integer coordinate range inside the AOI
MAX_RINGS = 6000


def to_date(v):
    s = str(int(v))
    return pd.Timestamp("%s-%s-%s" % (s[:4], s[4:6], s[6:]))


def basin_series(bidx, d0, d1):
    """Daily P30 and day-of-year threshold for one basin across the window."""
    st = np.load(os.path.join(cfg.DATA_DIR, "detection_state.npz"))
    thr = np.load(cfg.THRESHOLDS)
    dates = np.array([to_date(d) for d in st["dates"]])
    m = (dates >= d0) & (dates <= d1)
    dd = dates[m]
    pdoy = []
    for d in dd:
        doy = d.dayofyear
        pdoy.append(doy - 1 if (d.is_leap_year and doy >= 60) else doy)
    p30 = st["p30"][m, bidx]
    t20 = thr["thr20"][np.array(pdoy) - 1, bidx]
    step = 3
    return (np.array([int(x.strftime("%Y%m%d")) for x in dd])[::step].tolist(),
            np.nan_to_num(p30, nan=-1)[::step].round(1).tolist(),
            np.nan_to_num(t20, nan=-1)[::step].round(1).tolist())


def encode_fields(fields, aoi):
    """Simplified rings as integer coordinates on a GRID x GRID box inside the AOI."""
    utm = fields.estimate_utm_crs()
    g = fields.to_crs(utm).geometry.simplify(SIMPLIFY_M, preserve_topology=True)
    g = gpd.GeoSeries(g, crs=utm).to_crs("EPSG:4326")
    x0, y0, x1, y1 = aoi
    sx, sy = GRID / (x1 - x0), GRID / (y1 - y0)

    rings, ring_field = [], []
    for i, geom in enumerate(g):
        if geom.is_empty:
            continue
        polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
        for p in polys:
            xs, ys = np.asarray(p.exterior.coords).T
            ix = np.clip(((xs - x0) * sx).round(), 0, GRID).astype("int16")
            iy = np.clip(((y1 - ys) * sy).round(), 0, GRID).astype("int16")
            keep = np.ones(len(ix), bool)
            keep[1:] = (np.diff(ix) != 0) | (np.diff(iy) != 0)
            ix, iy = ix[keep], iy[keep]
            if len(ix) < 4:
                continue
            rings.append([int(v) for pair in zip(ix.tolist(), iy.tolist()) for v in pair])
            ring_field.append(i)
    return rings, ring_field


def build_demo(demo):
    key = demo["key"]
    need = ["fields.parquet", "ndvi.npz", "field_index.parquet", "aoi.json"]
    for n in need:
        if not os.path.exists(dc.demo_path(demo, n)):
            print("  skip %s - missing %s" % (key, n))
            return None

    meta = json.load(open(dc.demo_path(demo, "aoi.json")))
    fields = gpd.read_parquet(dc.demo_path(demo, "fields.parquet"))
    fidx = pd.read_parquet(dc.demo_path(demo, "field_index.parquet"))
    z = np.load(dc.demo_path(demo, "ndvi.npz"))

    aoi = meta["aoi"]
    dates = np.array([to_date(d) for d in z["dates"]])
    ndvi = z["ndvi"]
    scene_med = np.nanmedian(ndvi, axis=1)
    anom = ndvi - scene_med[:, None]
    a8 = np.clip(np.nan_to_num(anom, nan=0.0) * 100, -128, 127).astype("int8")
    # Cloud gaps are filled by linear interpolation along time, per parcel, between the
    # real observations either side. Without this the animation flickers to grey wherever
    # a composite was cloudy, which reads as "something happened here" when nothing did.
    # Leading and trailing gaps hold the nearest real value rather than extrapolating.
    # Parcels never seen at all stay grey, and `observed` still records what was measured.
    observed = np.isfinite(ndvi)
    obs_frac = observed.mean(axis=1).round(3).tolist()
    filled = ndvi.copy()
    xs = np.arange(ndvi.shape[0])
    for j in range(ndvi.shape[1]):
        ok = observed[:, j]
        if ok.sum() >= 2:
            filled[:, j] = np.interp(xs, xs[ok], ndvi[ok, j])
        elif ok.sum() == 1:
            filled[:, j] = ndvi[ok, j][0]
    ever_seen = observed.any(axis=0)
    print("    %s: filled %d of %d parcel-dates by interpolation (%d parcels never seen)"
          % (key, int((~observed & ever_seen[None, :]).sum()), observed.size,
             int((~ever_seen).sum())))
    w8 = np.clip(np.nan_to_num(z["water_frac"], nan=0.0) * 100, 0, 255).astype("uint8")
    # absolute NDVI drives how deep the colour is; 255 marks "no clear observation"
    n8 = np.where(np.isfinite(filled), np.clip(filled * 100, 0, 100), 255).astype("uint8")

    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    brow = basins[basins.HYBAS_ID == demo["hybas_id"]]
    bidx = int(brow.bidx.iloc[0])
    dts, p30, t20 = basin_series(bidx, dates.min(), dates.max())

    # Was the basin in drought on each composite date? The hue switches on this, so it is
    # read off the same daily P30-vs-threshold test the national map uses, sampled at the
    # nearest available day rather than re-derived.
    dts_i = np.array(dts)
    in_dr = []
    for d in dates:
        di = int(d.strftime("%Y%m%d"))
        j = int(np.argmin(np.abs(dts_i - di)))
        in_dr.append(1 if (p30[j] >= 0 and t20[j] >= 0 and p30[j] < t20[j]) else 0)

    ev = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"),
                     parse_dates=["start", "end", "peak_date"])
    ev = ev[(ev.bidx == bidx) & (ev.end >= dates.min()) & (ev.start <= dates.max())]

    rings, ring_field = encode_fields(fields, aoi)
    if len(rings) > MAX_RINGS:
        print("  %s: %d rings, keeping the %d largest fields" % (key, len(rings), MAX_RINGS))

    pivot = (fields["is_pivot"].to_numpy() if "is_pivot" in fields
             else np.zeros(len(fields), bool))
    tag = fields["mb_name"].to_numpy()
    # the second NDVI curve tracks the AOI's dominant land use rather than a crop label
    top_use = pd.Series(tag).value_counts().index[0]
    tagged_mask = tag == top_use
    # -1 marks "no clear observation"; JSON has no NaN literal
    tag_med = (np.nan_to_num(np.nanmedian(ndvi[:, tagged_mask], axis=1), nan=-1)
               .round(3).tolist() if tagged_mask.any() else [])

    # basin outline in AOI-local coords is not useful; ship it in lon/lat for the inset
    bgeo = brow.geometry.simplify(0.01, preserve_topology=True).iloc[0]
    bpolys = [bgeo] if bgeo.geom_type == "Polygon" else list(bgeo.geoms)
    binset = [[[round(v, 4) for v in c] for c in np.asarray(p.exterior.coords).tolist()]
              for p in bpolys]

    print("  %-14s %d fields, %d rings, %d composites, %d tagged"
          % (key, len(fields), len(rings), len(dates), int(tagged_mask.sum())))

    return {
        "key": key, "button": demo["button"], "metric_label": demo["metric_label"],
        "state": demo["state"], "year": demo["year"],
        "land_use": meta.get("land_use", {}),
        "hybas": str(demo["hybas_id"]), "note": demo["note"],
        "aoi": [round(v, 5) for v in aoi],
        "basin_bounds": [round(v, 4) for v in meta["basin_bounds"]],
        "basin_rings": binset,
        "grid": GRID,
        "rings": rings, "ring_field": ring_field,
        "tag": tag.tolist(),
        "area": fields["area_ha"].round(1).tolist(),
        "water_use": fidx["water_use"].fillna(0).round(3).tolist(),
        "green_index": fidx["green_index"].fillna(0).round(3).tolist(),
        "flood_index": fidx["flood_index"].fillna(0).round(3).tolist(),
        "signal": fidx["water_signal"].tolist(),
        "n_flood": int((fidx["water_signal"] == "flooding").sum()),
        "dates": [int(d.strftime("%Y%m%d")) for d in dates],
        "anom": [row.tolist() for row in a8],
        "wfrac": [row.tolist() for row in w8],
        "ndvi": [row.tolist() for row in n8],
        "in_drought": in_dr,
        # one flag per parcel, not per parcel-date: after interpolation the only parcels
        # without a value are those never seen in any composite
        "seen": np.where(ever_seen, 1, 0).astype("uint8").tolist(),
        "obs_frac": obs_frac,
        "peak_date": int(z["peak_date"]),
        "scene_ndvi": np.nan_to_num(scene_med, nan=-1).round(3).tolist(),
        "tag_ndvi": tag_med,
        "d_dates": dts, "p30": p30, "thr": t20,
        "events": [{"start": r.start.strftime("%Y-%m-%d"), "end": r.end.strftime("%Y-%m-%d"),
                    "days": int(r.duration_days), "peak": r.peak_date.strftime("%Y-%m-%d")}
                   for r in ev.itertuples()],
        "n_tagged": int(tagged_mask.sum()), "top_use": str(top_use),
        "pivot": pivot.astype("uint8").tolist(),
        "n_pivot": int(pivot.sum()),
    }


def build_payload():
    out = []
    for demo in dc.DEMOS:
        d = build_demo(demo)
        if d:
            out.append(d)
    return out


SECTION_CSS = """
.demo{margin:0 20px 28px}
.dbtns{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.dbtn{background:#fff;border:1px solid var(--line);border-radius:8px;padding:9px 13px;
font:inherit;font-size:13px;color:var(--ink);cursor:pointer;transition:.12s}
.dbtn:hover{border-color:var(--accent);color:var(--accent)}
.dbtn.on{background:var(--accent);border-color:var(--accent);color:#fff}
.dgrid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}
@media(max-width:1100px){.dgrid{grid-template-columns:1fr}}
.playbar{display:flex;gap:12px;align-items:center;margin:10px 0 4px}
.playbar button{background:var(--accent);color:#fff;border:0;border-radius:6px;
padding:6px 14px;font:inherit;font-size:13px;cursor:pointer}
.playbar input[type=range]{flex:1}
.dkey{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--dim);margin-top:8px}
.dkey i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;
vertical-align:-1px;border:1.5px solid}
.stat{display:flex;gap:22px;margin:6px 0 2px;font-size:12.5px}
.stat b{font-size:16px;display:block;font-weight:600}
.stat span{color:var(--dim)}
"""

SECTION_HTML = """
<div class="demo">
  <div class="card">
    <div style="font-weight:600;margin-bottom:2px">From basin to field: who kept growing while the rain stopped</div>
    <div class="sub" style="margin-bottom:11px">Each button opens the most extreme basin in Brazil on
    that drought metric that still sits on farmland, and zooms to a 24 km window on its densest cluster
    of agricultural parcels. Colour carries two things at once: <b>hue</b> is what the weather was doing
    on that date - <span style="color:#4a1d96">purple while the basin is in drought</span>,
    <span style="color:#14532d">green while it is not</span> - and <b>depth</b> is the parcel's own NDVI.
    So deep purple is a full canopy while the rain has failed, and light purple is bare ground in the
    same drought. Press Play to run the year.</div>
    <div class="dbtns" id="dbtns"></div>
    <div id="dbody" style="display:none">
      <div class="stat" id="dstat"></div>
      <div class="dgrid">
        <div>
          <svg id="dmap" viewBox="0 0 4000 4000"></svg>
          <div class="dkey">
            <span><i style="border-color:#4a1d96;background:#4a1d96"></i>high NDVI in drought</span>
            <span><i style="border-color:#c9bce4;background:#ede9f7"></i>low NDVI in drought</span>
            <span><i style="border-color:#14532d;background:#14532d"></i>high NDVI when wet</span>
            <span><i style="border-color:#bcd8c4;background:#e2f3e7"></i>low NDVI when wet</span>
            <span><i style="border-color:#0b6cf0;background:transparent;border-width:2px"></i>centre pivot (irrigated)</span>
            <span><i style="border-color:#d6dbe1;background:#eceff3"></i>never observed</span>
          </div>
        </div>
        <div>
          <svg id="dtrace" viewBox="0 0 820 360"></svg>
          <div class="sub" id="dnote" style="margin-top:6px"></div>
        </div>
      </div>
      <div class="playbar">
        <button id="dplay">Play</button>
        <input type="range" id="dtime" min="0" max="10" value="0">
        <span class="yr" id="dlab" style="min-width:96px"></span>
      </div>
    </div>
  </div>
</div>
"""

SECTION_JS = r"""
(async()=>{
const DEMOS = await unpack("__DEMO__");
if(!DEMOS.length) return;
const btns=document.getElementById("dbtns");
const body=document.getElementById("dbody");
const ns="http://www.w3.org/2000/svg";
let cur=null, paths=[], t=0, timer=null;

DEMOS.forEach((d,i)=>{
  const b=document.createElement("button");
  b.className="dbtn"; b.textContent=d.button;
  b.onclick=()=>{document.querySelectorAll(".dbtn").forEach(x=>x.classList.remove("on"));
    b.classList.add("on"); show(i)};
  btns.appendChild(b);
});

function ymd(n){const s=""+n;return s.slice(0,4)+"-"+s.slice(4,6)+"-"+s.slice(6)}

function show(i){
  cur=DEMOS[i]; body.style.display="block";
  // Open on the peak of the MAIN event - the longest one, which is what ndvi.npz
  // recorded - not on events[0], which is merely the earliest of the year. And prefer a
  // composite that was actually mostly cloud-free: landing on a date where half the
  // parcels are masked shows a grey map and hides the thing being demonstrated.
  const pk=cur.peak_date;
  let bi=0, bs=-1e12;
  cur.dates.forEach((d,k)=>{
    const days=Math.abs(d-pk)/100;                 // rough month-scale distance
    const score=(cur.obs_frac[k]||0)*2 - days*0.15;
    if(score>bs){bs=score;bi=k}
  });
  t=bi;
  const sv=document.getElementById("dmap"); sv.innerHTML=""; paths=[];
  // Crop to where the fields actually are. The AOI is a square in degrees but the fields
  // inside it are not, and drawing the full square left a large empty panel.
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  for(const a of cur.rings) for(let k=0;k<a.length;k+=2){
    if(a[k]<x0)x0=a[k]; if(a[k]>x1)x1=a[k];
    if(a[k+1]<y0)y0=a[k+1]; if(a[k+1]>y1)y1=a[k+1];
  }
  const pad=Math.max(x1-x0,y1-y0)*0.02;
  x0-=pad;y0-=pad;x1+=pad;y1+=pad;
  sv.setAttribute("viewBox",`${x0} ${y0} ${x1-x0} ${y1-y0}`);
  const bg=document.createElementNS(ns,"rect");
  bg.setAttribute("x",x0); bg.setAttribute("y",y0);
  bg.setAttribute("width",x1-x0); bg.setAttribute("height",y1-y0);
  bg.setAttribute("fill","#f2f5f8");
  sv.appendChild(bg);
  for(let r=0;r<cur.rings.length;r++){
    const a=cur.rings[r]; let d="M";
    for(let k=0;k<a.length;k+=2) d+=(k?"L":"")+a[k]+","+a[k+1];
    d+="Z";
    const p=document.createElementNS(ns,"path");
    p.setAttribute("d",d);
    const fi=cur.ring_field[r];
    // Centre pivots get their own outline. That is irrigation infrastructure, not crop
    // type: a circle in a field-boundary layer is a pump, and the whole question is
    // whether the parcels with pumps behave differently from the ones without.
    const pv=cur.pivot && cur.pivot[fi];
    p.setAttribute("stroke", pv ? "#0b6cf0" : "#2b333d");
    p.setAttribute("stroke-width", pv ? 16 : 5);
    p.setAttribute("fill","#ffffff");
    p._f=fi; paths.push(p); sv.appendChild(p);
  }
  const sl=document.getElementById("dtime");
  sl.max=cur.dates.length-1; sl.value=t;
  document.getElementById("dstat").innerHTML=
    `<div><b>${cur.hybas}</b><span>HydroBASINS lev 6</span></div>`+
    `<div><b>${cur.state} ${cur.year}</b><span>${cur.top_use} dominates this window</span></div>`+
    `<div><b>${cur.tag.length}</b><span>fields in the 24 km window</span></div>`+
    `<div><b>${cur.n_pivot}</b><span>centre pivots in view</span></div>`+
    `<div><b id="dstate">-</b><span>basin state on this date</span></div>`;
  document.getElementById("dnote").textContent=cur.note;
  frame(); drawTrace();
}

// Hue says what the WEATHER was doing on this date, depth says how green the field was.
// Purple = the basin was in drought; green = it was not. Deep = high NDVI, light = low.
// So a deep purple field is holding a full canopy while the rain has failed, which is the
// thing worth looking at; light green is a bare field in a normal wet spell, which is not.
const RAMP_DRY =[[237,233,247],[ 74, 29,150]];   // light purple -> deep purple
const RAMP_WET =[[226,243,231],[ 20, 83, 45]];   // light green  -> deep green
const ND_LO=0.15, ND_HI=0.75;                    // NDVI mapped onto that ramp

function mix(r,v){
  const a=r[0],b=r[1];
  return `rgb(${Math.round(a[0]+(b[0]-a[0])*v)},${Math.round(a[1]+(b[1]-a[1])*v)},`+
         `${Math.round(a[2]+(b[2]-a[2])*v)})`;
}

function frame(){
  const nd=cur.ndvi[t], ob=cur.seen;
  const dry=cur.in_drought[t];
  const ramp = dry ? RAMP_DRY : RAMP_WET;
  for(const p of paths){
    const f=p._f;
    if(!ob[f] || nd[f]===255){ p.setAttribute("fill","#eceff3"); continue }  // never observed
    const v=Math.max(0,Math.min(1,(nd[f]/100 - ND_LO)/(ND_HI - ND_LO)));
    p.setAttribute("fill", mix(ramp, v));
  }
  document.getElementById("dstate").textContent =
    dry ? "in drought" : "not in drought";
  document.getElementById("dstate").style.color = dry ? "#4a1d96" : "#14532d";
  document.getElementById("dlab").textContent=ymd(cur.dates[t]);
  cursor();
}

function drawTrace(){
  const sv=document.getElementById("dtrace"); sv.innerHTML="";
  const W=820,H=360,L=48,R=52,T=16,B=64;
  const add=(tp,at)=>{const e=document.createElementNS(ns,tp);
    for(const k in at)e.setAttribute(k,at[k]);sv.appendChild(e);return e};
  const dd=cur.d_dates, p30=cur.p30, thr=cur.thr;
  const mx=Math.max(1,...p30.filter(v=>v>=0),...thr.filter(v=>v>=0));
  const t0=+new Date(ymd(dd[0])), t1=+new Date(ymd(dd[dd.length-1]));
  const px=d=>L+(( +new Date(ymd(d))-t0)/(t1-t0))*(W-L-R);
  const py=v=>T+(1-Math.max(v,0)/mx)*(H-T-B);
  for(let g=0;g<=4;g++){const v=mx*g/4;
    add("line",{x1:L,x2:W-R,y1:py(v),y2:py(v),stroke:"#e8edf3"});
    add("text",{x:L-6,y:py(v)+4,fill:"#5f6b7a","font-size":11,"text-anchor":"end"}).textContent=Math.round(v)}
  add("text",{x:L-6,y:T-4,fill:"#5f6b7a","font-size":10,"text-anchor":"end"}).textContent="mm/30d";
  for(const e of cur.events){
    const xa=px(+e.start.replace(/-/g,"")), xb=px(+e.end.replace(/-/g,""));
    add("rect",{x:xa,y:T,width:Math.max(2,xb-xa),height:H-T-B,fill:"#c0392b","fill-opacity":.09});
  }
  const line=(xs,ys,col,w)=>add("path",{d:"M"+xs.map((x,i)=>px(x).toFixed(1)+","+ys[i].toFixed(1)).join("L"),
    fill:"none",stroke:col,"stroke-width":w});
  line(dd,thr.map(py),"#9aa5b3",1.4);
  line(dd,p30.map(py),"#1668c4",1.9);
  // NDVI on its own axis
  const ny=v=>T+(1-Math.max(v,0))*(H-T-B);
  const nd=cur.dates, sm=cur.scene_ndvi;
  const ok=[]; nd.forEach((d,i)=>{if(sm[i]>=0)ok.push(i)});
  line(ok.map(i=>nd[i]),ok.map(i=>ny(sm[i])),"#1a9850",1.9);
  if(cur.tag_ndvi.length){
    const ok2=[]; nd.forEach((d,i)=>{if(cur.tag_ndvi[i]>=0)ok2.push(i)});
    line(ok2.map(i=>nd[i]),ok2.map(i=>ny(cur.tag_ndvi[i])),
         "#b45309",1.9);
  }
  for(let g=0;g<=4;g++){const v=g/4;
    add("text",{x:W-R+6,y:ny(v)+4,fill:"#5f6b7a","font-size":11}).textContent=v.toFixed(2)}
  add("text",{x:W-R+6,y:T-4,fill:"#5f6b7a","font-size":10}).textContent="NDVI";
  add("text",{x:L,y:H-B+34,fill:"#1668c4","font-size":11}).textContent="30-day rainfall";
  add("text",{x:L+110,y:H-B+34,fill:"#9aa5b3","font-size":11}).textContent="drought threshold";
  add("text",{x:L+232,y:H-B+34,fill:"#1a9850","font-size":11}).textContent="NDVI, all fields";
  if(cur.tag_ndvi.length)
    add("text",{x:L+352,y:H-B+34,fill:"#b45309","font-size":11})
      .textContent="NDVI, "+cur.top_use;
  dd.forEach((d,i)=>{if(i%12===0)
    add("text",{x:px(d),y:H-B+16,fill:"#5f6b7a","font-size":10,"text-anchor":"middle"})
      .textContent=ymd(d).slice(0,7)});
  sv._px=px; sv._geom={T,H,B};
  cursor();
}

function cursor(){
  const sv=document.getElementById("dtrace");
  if(!sv._px) return;
  let c=sv.querySelector("#dcur");
  if(!c){c=document.createElementNS(ns,"line");c.setAttribute("id","dcur");
    c.setAttribute("stroke","#182029");c.setAttribute("stroke-width",1.4);sv.appendChild(c)}
  const x=sv._px(cur.dates[t]), g=sv._geom;
  c.setAttribute("x1",x);c.setAttribute("x2",x);
  c.setAttribute("y1",g.T);c.setAttribute("y2",g.H-g.B);
}

document.getElementById("dtime").oninput=e=>{t=+e.target.value;frame()};
document.getElementById("dplay").onclick=e=>{
  if(timer){clearInterval(timer);timer=null;e.target.textContent="Play";return}
  e.target.textContent="Pause";
  timer=setInterval(()=>{
    t=(t+1)%cur.dates.length;
    document.getElementById("dtime").value=t; frame();
  },320);
};
})();
"""

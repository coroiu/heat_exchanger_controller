#!/usr/bin/env python3
"""Fit RC ratios (UA/C, 1/RC, g/C) by minimising H-hour-ahead simulation error.

Uses hourly means of supply/outdoor/indoor and correctly-aligned Open-Meteo solar
(step k uses the slot labelled k+1 h). More robust than the one-step OLS in
estimate_rc.py. Needs numpy + scipy.
Usage: fit_rc_multistep.py [days=14] [H=3] [indoor_entity=the integration's indoor sensor]
Per-room seeds: fit_rc_multistep.py 14 3 sensor.my_room_temperature
"""
import sys, sqlite3, json, urllib.request
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
import numpy as np
from estimate_rc import fetch_history
import ha_local
DB=ha_local.db_path()
DEFAULT_INDOOR=ha_local.indoor_entity()
LAT,LON=ha_local.location()

def _utc(s): return datetime.fromisoformat(s).timestamp()
# (start, end) epoch ranges whose indoor data doesn't reflect the heating physics
# (open windows, fires, ...). Any hour overlapping one is dropped. Listed per install in
# tools/exclude.local.json (gitignored): [["2026-01-01T19:30:00+01:00", "2026-01-01T22:30:00+01:00", "window open"], ...]
def _load_exclude():
    p=os.path.join(os.path.dirname(os.path.abspath(__file__)),"exclude.local.json")
    if not os.path.exists(p): return []
    return [(_utc(a),_utc(b)) for a,b,*_ in json.load(open(p))]
EXCLUDE=_load_exclude()

def load_data(days, indoor_entity=DEFAULT_INDOOR):
    """Hourly arrays h, Tin (mean), Tin_p (point at hour start), Tout, Sup, I and the usable mask."""
    end=datetime.now(timezone.utc).timestamp(); start=end-days*86400
    conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True)
    def hourly_mean(eid):
        d=fetch_history(conn,eid,start-7200,end)
        ts=np.array([x[0] for x in d]); v=np.array([x[1] for x in d])
        grid=np.arange(np.ceil(start/3600)*3600, end, 300.0)  # 5-min ffill then hourly mean
        idx=np.searchsorted(ts,grid,side='right')-1
        g=np.where(idx>=0, v[np.clip(idx,0,None)], np.nan)
        hours=grid[::12][:len(grid)//12]
        return hours, g[:len(hours)*12].reshape(-1,12).mean(axis=1)
    h, Tin = hourly_mean(indoor_entity)
    _, Tout = hourly_mean(ha_local.outdoor_entity())
    _, Sup = hourly_mean(ha_local.supply_entity())
    # Tin at hour boundaries (point) for state
    def point(eid):
        d=fetch_history(conn,eid,start-7200,end); ts=np.array([x[0] for x in d]); v=np.array([x[1] for x in d])
        idx=np.searchsorted(ts,h,side='right')-1; return v[idx]
    Tin_p=point(indoor_entity)
    sd=datetime.fromtimestamp(start-86400,tz=timezone.utc).strftime("%Y-%m-%d"); ed=datetime.fromtimestamp(end,tz=timezone.utc).strftime("%Y-%m-%d")
    url=f"https://archive-api.open-meteo.com/v1/archive?latitude={LAT}&longitude={LON}&start_date={sd}&end_date={ed}&hourly=shortwave_radiation&timezone=UTC"
    try:
        p=json.loads(urllib.request.urlopen(url,timeout=30).read())
    except Exception:
        url=url.replace("archive-api","api").replace("/v1/archive","/v1/forecast").replace(f"start_date={sd}&end_date={ed}",f"past_days={days+2}&forecast_days=1")
        p=json.loads(urllib.request.urlopen(url,timeout=30).read())
    sol={datetime.strptime(t,"%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp():(w or 0) for t,w in zip(p["hourly"]["time"],p["hourly"]["shortwave_radiation"])}
    # step k covers [h_k, h_k+1): mean irradiance is the slot labelled h_k+1
    I=np.array([sol.get(t+3600,np.nan) for t in h])
    ok=~(np.isnan(Tin)|np.isnan(Tout)|np.isnan(Sup)|np.isnan(I)|np.isnan(Tin_p))
    excluded=np.zeros(len(h),bool)
    for a,b in EXCLUDE: excluded|=(h<b)&(h+3600>a)
    ok&=~excluded
    return dict(h=h,Tin=Tin,Tin_p=Tin_p,Tout=Tout,Sup=Sup,I=I,ok=ok,excluded=int(excluded.sum()))

def main():
    days=int(sys.argv[1]) if len(sys.argv) > 1 else 14; H=int(sys.argv[2]) if len(sys.argv) > 2 else 3
    indoor=sys.argv[3] if len(sys.argv) > 3 else DEFAULT_INDOOR
    D=load_data(days,indoor); h,Tin_p,Tout,Sup,I,ok=D["h"],D["Tin_p"],D["Tout"],D["Sup"],D["I"],D["ok"]
    print("indoor",indoor,"hours",len(h),"usable",ok.sum(),"excluded",D["excluded"],"solar nan",np.isnan(I).sum())
    def sim_err(params, internal=False):
        ua,l,g = params[:3]; q = params[3] if internal else 0.0
        errs=[]
        for k in range(len(h)-H):
            if not ok[k:k+H+1].all(): continue
            t=Tin_p[k]
            for j in range(H):
                t=t+ua*(Sup[k+j]-t)+l*(Tout[k+j]-t)+g*I[k+j]+q
            errs.append(Tin_p[k+H]-t)
        return np.array(errs)
    from scipy.optimize import least_squares
    cur=[23.5/500, 1/(0.048*500), 0.6/500]  # pre-2026-10-11 params, used as baseline + start point
    e=sim_err(cur); print("BASELINE ua/C=%.4f 1/RC=%.4f g/C=%.5f  3h err mean=%+.3f rms=%.3f n=%d  UA*R=%.2f"%(*cur,e.mean(),np.sqrt((e**2).mean()),len(e),cur[0]/cur[1]))
    for internal in [False, True]:
        x0=cur+([0.0] if internal else [])
        r=least_squares(lambda p: sim_err(p,internal), x0, bounds=([1e-4,1e-4,0]+([-1] if internal else []),[0.5,0.5,0.01]+([1] if internal else [])))
        e=sim_err(r.x,internal); ua,l,g=r.x[:3]
        print("FIT internal=%s ua/C=%.4f 1/RC=%.4f g/C=%.5f %s 3h err mean=%+.3f rms=%.3f UA*R=%.2f  -> C=500: UA=%.1f R=%.4f g=%.2f"%(internal,ua,l,g,("q=%.4f°C/h"%r.x[3]) if internal else "",e.mean(),np.sqrt((e**2).mean()),ua/l,ua*500,1/(l*500),g*500))

if __name__ == "__main__":
    main()

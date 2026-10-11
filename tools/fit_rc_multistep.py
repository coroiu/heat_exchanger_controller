#!/usr/bin/env python3
"""Fit RC ratios (UA/C, 1/RC, g/C) by minimising H-hour-ahead simulation error.

Uses hourly means of supply/outdoor/indoor and correctly-aligned Open-Meteo solar
(step k uses the slot labelled k+1 h). More robust than the one-step OLS in
estimate_rc.py. Needs numpy + scipy.  Usage: fit_rc_multistep.py [days=14] [H=3]
"""
import sys, sqlite3, json, urllib.request
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
import numpy as np
from estimate_rc import fetch_history
DB="/path/to/ha/config/home-assistant_v2.db"
days=int(sys.argv[1]) if len(sys.argv) > 1 else 14; H=int(sys.argv[2]) if len(sys.argv) > 2 else 3
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
h, Tin = hourly_mean("sensor.indoor_steer_temperature")
_, Tout = hourly_mean("sensor.aqara_sensor_03_temperature")
_, Sup = hourly_mean("sensor.sonoff_sensor_06_temperature")
# Tin at hour boundaries (point) for state
def point(eid):
    d=fetch_history(conn,eid,start-7200,end); ts=np.array([x[0] for x in d]); v=np.array([x[1] for x in d])
    idx=np.searchsorted(ts,h,side='right')-1; return v[idx]
Tin_p=point("sensor.indoor_steer_temperature")
sd=datetime.fromtimestamp(start-86400,tz=timezone.utc).strftime("%Y-%m-%d"); ed=datetime.fromtimestamp(end,tz=timezone.utc).strftime("%Y-%m-%d")
url=f"https://archive-api.open-meteo.com/v1/archive?latitude=LATITUDE&longitude=LONGITUDE&start_date={sd}&end_date={ed}&hourly=shortwave_radiation&timezone=UTC"
try:
    p=json.loads(urllib.request.urlopen(url,timeout=30).read())
except Exception:
    url=url.replace("archive-api","api").replace("/v1/archive","/v1/forecast").replace(f"start_date={sd}&end_date={ed}",f"past_days={days+2}&forecast_days=1")
    p=json.loads(urllib.request.urlopen(url,timeout=30).read())
sol={datetime.strptime(t,"%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp():(w or 0) for t,w in zip(p["hourly"]["time"],p["hourly"]["shortwave_radiation"])}
# step k covers [h_k, h_k+1): mean irradiance is the slot labelled h_k+1
I=np.array([sol.get(t+3600,np.nan) for t in h])
ok=~(np.isnan(Tin)|np.isnan(Tout)|np.isnan(Sup)|np.isnan(I)|np.isnan(Tin_p))
print("hours",len(h),"usable",ok.sum(), "solar nan",np.isnan(I).sum())
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

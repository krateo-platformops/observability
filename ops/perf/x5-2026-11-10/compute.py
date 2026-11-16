import json,sys
rows=[json.loads(l) for l in open((sys.argv[1] if len(sys.argv) > 1 else __file__.rsplit("/", 1)[0] + "/span-metrics-rows.jsonl"))]
def pct(buckets,bounds,q):
    n=sum(buckets); 
    if n==0: return "-"
    k=q*n; c=0
    for i,b in enumerate(buckets):
        c+=b
        if c>=k:
            lo=bounds[i-1] if i>0 else 0; hi=bounds[i] if i<len(bounds) else float("inf")
            return f"({lo:g},{hi:g}]"
def agg(svc,lo,hi,names=None):
    tot=0; err=0; b=None; bounds=None; s500=0
    for r in rows:
        if r["ServiceName"]!=svc or not (lo<=r["ts"]<=hi): continue
        if names and r["sn"] not in names: continue
        tot+=int(r["Count"]); 
        if r["err"]=="true": err+=int(r["Count"])
        if r["sc"].startswith("5"): s500+=int(r["Count"])
        bc=[int(x) for x in r["BucketCounts"]]
        b=bc if b is None else [x+y for x,y in zip(b,bc)]; bounds=r["ExplicitBounds"]
    return tot,err,s500,(pct(b,bounds,.5) if b else "-"),(pct(b,bounds,.95) if b else "-")
W=[("A steps1-3 API","snowplow","2026-11-10 13:56:52","2026-11-10 14:17:50",None),
   ("A full stress API","snowplow","2026-11-10 13:56:52","2026-11-10 14:28:00",None),
   ("A step4 API","snowplow","2026-11-10 14:18:15","2026-11-10 14:28:00",None),
   ("B composition.open","krateo-frontend","2026-11-10 15:12:00","2026-11-10 15:41:00",["krateo.composition.open"]),
   ("B form.submit","krateo-frontend","2026-11-10 15:12:00","2026-11-10 15:41:00",["krateo.form.submit"]),
   ("B widget.fetch","krateo-frontend","2026-11-10 15:12:00","2026-11-10 15:41:00",["krateo.widget.fetch"]),
   ("PL page.load","krateo-frontend","2026-11-11 09:25:00","2026-11-11 10:05:00",["krateo.page.load"])]
print("window\tcalls\tsli_err\t5xx\tavail%\tp50(ms)\tp95(ms)")
for name,svc,lo,hi,nm in W:
    t,e,s5,p50,p95=agg(svc,lo,hi,nm)
    print(f"{name}\t{t}\t{e}\t{s5}\t{(100*(t-e)/t if t else 0):.3f}\t{p50}\t{p95}")

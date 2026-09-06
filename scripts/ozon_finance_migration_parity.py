"""Сверка старого и нового finance API по всем датам периода проекта.
Делается ДО отключения 2026-09-08; результат сохраняется навсегда."""
import json,os,sys,time
from collections import defaultdict
from datetime import date,timedelta
sys.path.insert(0,'/Users/mihaileliseev/mp-analytics')
from dotenv import load_dotenv; load_dotenv("/Users/mihaileliseev/mp-analytics/.env")
from loaders import http_retry
OUT="/private/tmp/claude-501/-Users-mihaileliseev-mp-analytics/aa40a0dd-8668-4802-8516-65cf0cc44048/scratchpad"
H={"Client-Id":os.getenv("OZON_CLIENT_ID"),"Api-Key":os.getenv("OZON_API_KEY"),"Content-Type":"application/json"}
B="https://api-seller.ozon.ru"
FROM,TO=date(2026,3,28),date(2026,9,4)
# соответствие: старое имя операции -> новый type_id
PAIRS={"OperationMarketplaceCostPerClick":41,"OperationPromotionWithCostPerOrder":54,
       "MarketplaceRedistributionOfAcquiringOperation":1,"PremiumMembership":51,
       "InsuranceServiceSellerItem":76,"OperationMarketplaceAcceleratedProductReviews":96,
       "StarsMembership":74,"OperationMarketplaceServiceStorage":46,
       "OperationMarketplacePackageMaterialsProvision":38,"OperationMarketplacePackageRedistribution":39,
       "TemporaryStorage":78,"OperationMarketPlaceItemPinReview":61,"OperationPointsForReviews":47}
def old_day(d):
    acc=defaultdict(float); page=1
    while True:
        p={"filter":{"date":{"from":f"{d}T00:00:00.000Z","to":f"{d}T23:59:59.999Z"},
           "operation_type":[],"posting_number":"","transaction_type":"all"},"page":page,"page_size":1000}
        r=http_retry.post(f"{B}/v3/finance/transaction/list",label="old",headers=H,json=p,timeout=180)
        if r.status_code!=200: break
        b=((r.json() or {}).get("result") or {}).get("operations") or []
        for x in b: acc[str(x.get("operation_type") or "")]+=float(x.get("amount") or 0)
        if len(b)<1000: break
        page+=1
    return acc
def new_day(d):
    acc=defaultdict(float); last=None
    def num(x):
        try: return float((x or {}).get("amount") or 0)
        except Exception: return 0.0
    while True:
        body={"date":d}
        if last: body["last_id"]=last
        r=http_retry.post(f"{B}/v1/finance/accrual/by-day",label="new",headers=H,json=body,timeout=180)
        if r.status_code!=200: break
        j=r.json()
        for a in (j.get("accruals") or []):
            p=a.get("posting") or {}
            for pr in (p.get("products") or []):
                for blk in ("delivery","commission"):
                    for s in ((pr.get(blk) or {}).get("services") or []): acc[s.get("type_id")]+=num(s.get("accrued"))
            for f in ((a.get("item_fees") or {}).get("fees") or []):
                for s in (f.get("fees") or []): acc[s.get("type_id")]+=num(s.get("accrued"))
            nif=a.get("non_item_fee")
            if isinstance(nif,dict):
                for s in (nif.get("fees") or []): acc[s.get("type_id")]+=num(s.get("accrued"))
                if nif.get("type_id") is not None: acc[nif["type_id"]]+=num(nif.get("accrued"))
            cf=a.get("container_fees")
            if isinstance(cf,dict):
                for s in (cf.get("fees") or []): acc[s.get("type_id")]+=num(s.get("accrued"))
        last=j.get("last_id")
        if not (j.get("accruals") or []) or not last: break
    return acc
res={}; d=FROM; t0=time.time(); n=0
while d<=TO:
    ds=d.isoformat(); o=old_day(ds); nw=new_day(ds)
    row={}
    for name,tid in PAIRS.items():
        row[name]={"old":round(o.get(name,0.0),2),"new":round(nw.get(tid,0.0),2),
                   "diff":round(nw.get(tid,0.0)-o.get(name,0.0),2)}
    res[ds]=row; n+=1
    if n%20==0: print(f"  {ds}  ({n} дат, {time.time()-t0:.0f}с)",flush=True)
    d+=timedelta(days=1)
json.dump({"built_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
           "pairs":PAIRS,"per_day":res},open(f"{OUT}/migration_parity.json","w"),ensure_ascii=False)
print(f"\n{'операция':<46}{'старый':>16}{'новый':>16}{'разница':>14}{'дат с расх.':>12}")
for name,tid in PAIRS.items():
    so=sum(res[d][name]["old"] for d in res); sn=sum(res[d][name]["new"] for d in res)
    bad=sum(1 for d in res if abs(res[d][name]["diff"])>0.5)
    print(f"{name[:44]:<46}{so:>16,.2f}{sn:>16,.2f}{sn-so:>14,.2f}{bad:>12}")
print(f"\nдат сверено: {n}, результат сохранён в migration_parity.json")

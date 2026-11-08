"""
Synthetic SAP EWM sample data for a travel-reduction / KPI-dashboard demo.

Simulates an embedded S/4HANA EWM warehouse (WH01, automotive spare parts DC):
  - storage bins with X/Y/Z coordinates (as in /SCWM/LAGP)
  - products with ABC class
  - 8 weeks of confirmed outbound pick warehouse tasks (shape of /SCWM/ORDIM_C)
  - weeks 1-4 = baseline, week 5 = cut-over of the new storage type search
    sequence + slotting, weeks 6-8 = steady state after the change.

ALL NUMBERS ARE SYNTHETIC. They illustrate the method and the dashboard, not a
real result. Replace the CSVs with your own /SCWM/MON extract to get real KPIs.

Run:  python3 generate_sample.py
"""
import csv, json, math, os, random, datetime as dt
from collections import defaultdict

random.seed(20261108)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")
os.makedirs(OUT, exist_ok=True)

LGNUM = "WH01"
# ---------- layout ----------
AISLE_PITCH = 3.6   # metres between aisle centre lines
STACK_PITCH = 1.4   # metres between bin columns along an aisle
PICKFACE_AISLES = range(1, 13)     # aisles 1-12 have shelving at level 1 (0020)
HIGHRACK_AISLES = range(1, 21)     # aisles 1-20 pallet rack levels 2-5 (0010); 13-20 all levels
BULK_AISLES = range(21, 25)        # floor bulk 0030
STACKS = 36
LEVELS = 5
ST_DESC = {"0010": "High rack (pallet)", "0020": "Pick face (shelving)", "0030": "Floor bulk"}

bins = []  # dict rows
for a in range(1, 21):
    for s in range(1, STACKS + 1):
        for l in range(1, LEVELS + 1):
            st = "0020" if (l == 1 and a in PICKFACE_AISLES) else "0010"
            bins.append(dict(LGNUM=LGNUM, LGPLA=f"{a:02d}-{s:02d}-{l:02d}", LGTYP=st,
                             AISLE=a, STACK=s, LEVEL=l,
                             X_CORD=round(a * AISLE_PITCH, 1), Y_CORD=round(s * STACK_PITCH, 1),
                             Z_CORD=round((l - 1) * 1.6, 1)))
for a in BULK_AISLES:
    for s in range(1, 21):
        bins.append(dict(LGNUM=LGNUM, LGPLA=f"{a:02d}-{s:02d}-01", LGTYP="0030",
                         AISLE=a, STACK=s, LEVEL=1,
                         X_CORD=round(a * AISLE_PITCH, 1), Y_CORD=round(s * STACK_PITCH * 1.8, 1), Z_CORD=0.0))
bin_by_id = {b["LGPLA"]: b for b in bins}

# ---------- products ----------
NAMES = ["Brake pad set", "Oil filter", "Air filter", "Cabin filter", "Spark plug", "Wiper blade",
         "Brake disc", "Shock absorber", "Timing belt", "Water pump", "Alternator", "Starter motor",
         "Headlamp bulb", "Fuel pump", "Clutch kit", "Radiator hose", "Thermostat", "Wheel bearing",
         "CV joint", "Tie rod end", "Control arm", "Coolant 1L", "Engine oil 5L", "Brake fluid 500ml",
         "Battery 60Ah", "Fuse kit", "Relay", "Oxygen sensor", "ABS sensor", "Ignition coil"]
N_PROD = 600
weights = sorted([math.exp(random.gauss(0, 1.15)) for _ in range(N_PROD)], reverse=True)
tot_w = sum(weights)
products = []
cum = 0.0
for i, w in enumerate(weights):
    cum += w / tot_w
    abc = "A" if cum <= 0.70 else ("B" if cum <= 0.95 else "C")
    products.append(dict(MATNR=f"TVK-{10000 + i}", MAKTX=f"{NAMES[i % len(NAMES)]} {i // len(NAMES) + 1:02d}",
                         ABC=abc, weight=w))
n_abc = defaultdict(int)
for p in products:
    n_abc[p["ABC"]] += 1

# ---------- slotting: bin per product, before and after ----------
def dist_from_base(b):  # front cross-aisle routing from packing/GI point at (0,0)
    return b["X_CORD"] + b["Y_CORD"]

all_bins = bins[:]
random.shuffle(all_bins)
level1_any = [b for b in bins if b["LEVEL"] == 1]
# BEFORE: putaway "next empty bin" over years -> near random. 62% of A-items and 45% of B-items happen to sit at level 1, but spread over the whole building.
used = set()
for p in products:
    pool = level1_any if (p["ABC"] in ("A", "B") and random.random() < (0.62 if p["ABC"] == "A" else 0.45)) else all_bins
    while True:
        b = random.choice(pool)
        if b["LGPLA"] not in used:
            used.add(b["LGPLA"]); p["BIN_BEFORE"] = b["LGPLA"]; break
# AFTER: slotting -> A to nearest pick-face bins, B next ring, C remainder of pick face then high rack lvl 2
pickface = sorted([b for b in bins if b["LGTYP"] == "0020"], key=dist_from_base)
highrack_low = sorted([b for b in bins if b["LGTYP"] == "0010" and b["LEVEL"] <= 2], key=dist_from_base)
order = sorted(products, key=lambda p: -p["weight"])
pf_iter = iter(pickface)
hr_iter = iter(highrack_low)
for p in order:
    if p["ABC"] in ("A", "B"):
        p["BIN_AFTER"] = next(pf_iter)["LGPLA"]
    else:
        try:
            p["BIN_AFTER"] = next(pf_iter)["LGPLA"] if random.random() < 0.35 else next(hr_iter)["LGPLA"]
        except StopIteration:
            p["BIN_AFTER"] = next(hr_iter)["LGPLA"]

# ---------- simulation ----------
START = dt.date(2026, 9, 7)           # Monday, week 1
CUTOVER = dt.date(2026, 10, 5)        # Monday, week 5: new STS + slotting live
N_WEEKS = 8
PICKERS = [f"PICK{i:02d}" for i in range(1, 15)]
WALK_MPS = 1.0                        # walking speed with cart
HANDLE_BASE = 28                      # s per pick line at shelf
HANDLE_HIGHRACK = 35                  # extra s for level>=2 (order picker truck)
HANDLE_BULK = 15                      # extra s for bulk
INDIRECT = 1.18                       # indirect time factor on direct labour

def route_len(bins_in_order):
    """Manhattan route with a front cross-aisle: base (0,0) -> bins -> base."""
    x, y, d = 0.0, 0.0, 0.0
    for b in bins_in_order:
        bx, by = b["X_CORD"], b["Y_CORD"]
        if abs(bx - x) < 1e-9:
            d += abs(by - y)
        else:
            d += y + abs(bx - x) + by
        x, y = bx, by
    d += y + x
    return d

prod_weights = [p["weight"] for p in products]
tanum = 1000000
who_no = 2000000
wt_rows = []
day_rows = []
for wk in range(N_WEEKS):
    for dow in range(5):
        day = START + dt.timedelta(days=wk * 7 + dow)
        after = day >= CUTOVER
        # week 5 is a ramp: half the orders still use old bins until Wednesday
        ramp = 1.0 if (not after or wk >= 5) else (0.4 + 0.2 * dow)
        n_wo = int(random.gauss(112, 9))
        n_wo = max(80, n_wo)
        completed_by_cutoff = 0
        for _ in range(n_wo):
            k = random.randint(3, 8)
            picks = random.choices(products, weights=prod_weights, k=k)
            use_new = after and random.random() < ramp
            bl = [bin_by_id[p["BIN_AFTER"] if use_new else p["BIN_BEFORE"]] for p in picks]
            # pick path sorted by bin sort key (aisle, stack): /SCWM/SBST style
            seq = sorted(zip(picks, bl), key=lambda t: (t[1]["AISLE"], t[1]["STACK"]))
            L = route_len([b for _, b in seq])
            travel_per_wt = L / k
            shift = 0 if random.random() < 0.55 else 1
            t0 = dt.datetime.combine(day, dt.time(6 if shift == 0 else 14, 0)) + dt.timedelta(minutes=random.randint(5, 440))
            who_no += 1
            processor = random.choice(PICKERS)
            t = t0
            for p, b in seq:
                tanum += 1
                handle = HANDLE_BASE + (HANDLE_HIGHRACK if (b["LGTYP"] == "0010" and b["LEVEL"] >= 2) else 0) \
                         + (HANDLE_BULK if b["LGTYP"] == "0030" else 0)
                secs = travel_per_wt / WALK_MPS + handle
                secs *= random.uniform(0.85, 1.15)
                r = random.random()
                p_bidi = 0.011 if use_new else 0.024
                exc = "BIDI" if r < p_bidi else ("DIFF" if r < p_bidi + 0.006 else "")
                if exc == "BIDI":
                    secs += 240  # bin denial: search + supervisor
                t = t + dt.timedelta(seconds=secs)
                wt_rows.append(dict(
                    LGNUM=LGNUM, TANUM=tanum, WHO=who_no, PROCTY="2010", QUEUE="OUT-PICK",
                    MATNR=p["MATNR"], ABC=p["ABC"], VSOLA=random.randint(1, 6), MEINS="EA",
                    VLTYP=b["LGTYP"], VLPLA=b["LGPLA"], NLTYP="9020", NLPLA="PACK-01",
                    CREATED_AT=t0.strftime("%Y-%m-%d %H:%M:%S"), CONFIRMED_AT=t.strftime("%Y-%m-%d %H:%M:%S"),
                    PROCESSOR=processor, EXCCODE=exc, TRAVEL_M=round(travel_per_wt, 1),
                    LABOUR_S=round(secs, 0), WEEK=wk + 1, PHASE="after" if use_new else "before"))
            if t.time() <= dt.time(16, 0) or shift == 1:
                completed_by_cutoff += 1
        # dock-to-stock (inbound) - out of scope of the change, tracked for the dashboard
        d2s = [max(1.2, random.lognormvariate(math.log(5.4), 0.35)) for _ in range(random.randint(18, 30))]
        open_wt_4h = int(random.gauss(21 if (after and wk >= 5) else 37, 5))
        day_rows.append(dict(DATE=day.isoformat(), WEEK=wk + 1, PHASE="after" if after else "before",
                             WAREHOUSE_ORDERS=n_wo, CUTOFF_COMPLIANCE=round(completed_by_cutoff / n_wo, 4),
                             INBOUND_DELIVERIES=len(d2s), DOCK_TO_STOCK_H=round(sum(d2s) / len(d2s), 2),
                             OPEN_WT_OVER_4H=max(0, open_wt_4h)))

# ---------- write CSVs ----------
def write_csv(name, rows, fields):
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

write_csv("bins_LAGP.csv", bins, ["LGNUM", "LGPLA", "LGTYP", "AISLE", "STACK", "LEVEL", "X_CORD", "Y_CORD", "Z_CORD"])
write_csv("products.csv", products, ["MATNR", "MAKTX", "ABC", "BIN_BEFORE", "BIN_AFTER"])
write_csv("warehouse_tasks_ORDIM_C.csv", wt_rows,
          ["LGNUM", "TANUM", "WHO", "PROCTY", "QUEUE", "MATNR", "ABC", "VSOLA", "MEINS", "VLTYP", "VLPLA",
           "NLTYP", "NLPLA", "CREATED_AT", "CONFIRMED_AT", "PROCESSOR", "EXCCODE", "TRAVEL_M", "LABOUR_S", "WEEK", "PHASE"])
write_csv("daily_kpis.csv", day_rows, ["DATE", "WEEK", "PHASE", "WAREHOUSE_ORDERS", "CUTOFF_COMPLIANCE",
                                        "INBOUND_DELIVERIES", "DOCK_TO_STOCK_H", "OPEN_WT_OVER_4H"])

# ---------- aggregates for the dashboard ----------
def mean(xs): return sum(xs) / len(xs) if xs else 0.0

weekly = []
for wk in range(1, N_WEEKS + 1):
    rows = [r for r in wt_rows if r["WEEK"] == wk]
    days = [d for d in day_rows if d["WEEK"] == wk]
    labour_h = sum(r["LABOUR_S"] for r in rows) / 3600 * INDIRECT
    weekly.append(dict(week=wk, start=(START + dt.timedelta(days=(wk - 1) * 7)).isoformat(),
                       phase="after" if wk >= 5 else "before", picks=len(rows),
                       travel_m=round(mean([r["TRAVEL_M"] for r in rows]), 1),
                       labour_h=round(labour_h, 0),
                       picks_per_lh=round(len(rows) / labour_h, 1),
                       exc_rate=round(sum(1 for r in rows if r["EXCCODE"]) / len(rows), 4),
                       bidi_rate=round(sum(1 for r in rows if r["EXCCODE"] == "BIDI") / len(rows), 4),
                       cutoff=round(mean([d["CUTOFF_COMPLIANCE"] for d in days]), 4),
                       d2s_h=round(mean([d["DOCK_TO_STOCK_H"] for d in days]), 2),
                       open4h=round(mean([d["OPEN_WT_OVER_4H"] for d in days]), 1)))

def phase_stats(ph):
    rows = [r for r in wt_rows if r["PHASE"] == ph]
    wks = {r["WEEK"] for r in rows}
    labour_h = sum(r["LABOUR_S"] for r in rows) / 3600 * INDIRECT
    return dict(picks=len(rows), weeks=len(wks), travel_m=round(mean([r["TRAVEL_M"] for r in rows]), 1),
                secs_per_pick=round(mean([r["LABOUR_S"] for r in rows]) * INDIRECT, 1),
                picks_per_lh=round(len(rows) / labour_h, 1),
                exc_rate=round(sum(1 for r in rows if r["EXCCODE"]) / len(rows), 4),
                highrack_share=round(sum(1 for r in rows if r["VLTYP"] == "0010" and int(r["VLPLA"][-2:]) >= 2) / len(rows), 4))

# steady state after = weeks 6-8 only (week 5 is the ramp)
before_rows = [r for r in wt_rows if r["WEEK"] <= 4]
after_rows = [r for r in wt_rows if r["WEEK"] >= 6]
def stats(rows):
    labour_h = sum(r["LABOUR_S"] for r in rows) / 3600 * INDIRECT
    return dict(picks=len(rows), travel_m=round(mean([r["TRAVEL_M"] for r in rows]), 1),
                secs_per_pick=round(mean([r["LABOUR_S"] for r in rows]) * INDIRECT, 1),
                picks_per_lh=round(len(rows) / labour_h, 1), labour_h=round(labour_h, 0),
                exc_rate=round(sum(1 for r in rows if r["EXCCODE"]) / len(rows), 4),
                bidi_rate=round(sum(1 for r in rows if r["EXCCODE"] == "BIDI") / len(rows), 4),
                highrack_share=round(sum(1 for r in rows if r["VLTYP"] == "0010" and int(r["VLPLA"][-2:]) >= 2) / len(rows), 4),
                pickface_share=round(sum(1 for r in rows if r["VLTYP"] == "0020") / len(rows), 4))
summary = dict(before=stats(before_rows), after=stats(after_rows), weeks_before=4, weeks_after=3)

# picks by storage type x ABC x phase
st_abc = defaultdict(int)
for r in before_rows: st_abc[("before", r["ABC"], r["VLTYP"])] += 1
for r in after_rows: st_abc[("after", r["ABC"], r["VLTYP"])] += 1
st_mix = [dict(phase=k[0], abc=k[1], st=k[2], picks=v) for k, v in sorted(st_abc.items())]

# aisle heat (picks per aisle per week, by phase)
aisle = defaultdict(int)
for r in before_rows: aisle[("before", bin_by_id[r["VLPLA"]]["AISLE"])] += 1
for r in after_rows: aisle[("after", bin_by_id[r["VLPLA"]]["AISLE"])] += 1
aisle_heat = [dict(phase=ph, aisle=a, picks_per_week=round(aisle[(ph, a)] / (4 if ph == "before" else 3), 0))
              for ph in ("before", "after") for a in range(1, 25)]

# top A items
per_prod = defaultdict(lambda: dict(before=[], after=[]))
for r in before_rows: per_prod[r["MATNR"]]["before"].append(r["TRAVEL_M"])
for r in after_rows: per_prod[r["MATNR"]]["after"].append(r["TRAVEL_M"])
pm = {p["MATNR"]: p for p in products}
top = sorted(per_prod.items(), key=lambda kv: -len(kv[1]["before"]))[:15]
top_items = []
for m, d in top:
    p = pm[m]
    bb, ba = bin_by_id[p["BIN_BEFORE"]], bin_by_id[p["BIN_AFTER"]]
    top_items.append(dict(matnr=m, desc=p["MAKTX"], abc=p["ABC"], picks_per_week=round(len(d["before"]) / 4, 0),
                          bin_before=p["BIN_BEFORE"], st_before=bb["LGTYP"], bin_after=p["BIN_AFTER"], st_after=ba["LGTYP"],
                          travel_before=round(mean(d["before"]), 1), travel_after=round(mean(d["after"]), 1) if d["after"] else None))

daily = [dict(date=d["DATE"], week=d["WEEK"], phase=d["PHASE"], cutoff=d["CUTOFF_COMPLIANCE"],
              d2s=d["DOCK_TO_STOCK_H"], open4h=d["OPEN_WT_OVER_4H"], wos=d["WAREHOUSE_ORDERS"]) for d in day_rows]

agg = dict(generated=dt.date.today().isoformat(), warehouse=LGNUM, cutover=CUTOVER.isoformat(),
           assumptions=dict(walk_mps=WALK_MPS, handle_base_s=HANDLE_BASE, handle_highrack_s=HANDLE_HIGHRACK,
                            handle_bulk_s=HANDLE_BULK, indirect_factor=INDIRECT, working_days=250),
           products=dict(total=N_PROD, A=n_abc["A"], B=n_abc["B"], C=n_abc["C"]),
           summary=summary, weekly=weekly, st_mix=st_mix, aisle_heat=aisle_heat, top_items=top_items, daily=daily,
           st_desc=ST_DESC)
with open(os.path.join(OUT, "aggregates.json"), "w") as f:
    json.dump(agg, f, indent=1)

print(json.dumps(dict(summary=summary, products=agg["products"], wt_rows=len(wt_rows)), indent=1))
for w in weekly: print(w)

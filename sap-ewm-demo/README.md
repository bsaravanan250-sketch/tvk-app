# SAP EWM travel-reduction demo (embedded S/4HANA EWM)

Sample data, a KPI dashboard page and a business case for two linked improvements:

1. Storage type search sequence tuning plus A/B re-slotting (Basic EWM, no new licence).
2. A weekly warehouse KPI dashboard that proves the result.

**All data here is synthetic.** It is shaped like an EWM extract so the method and the
dashboard can be shown; it is not a real result. Replace the CSVs with your own
warehouse monitor (/SCWM/MON) extract to get your site's numbers.

## Files

| Path | What it is |
| --- | --- |
| `generate_sample.py` | Simulation: 600 products, 3,680 bins with X/Y/Z coordinates, 8 weeks of pick tasks. Weeks 1-4 baseline, week 5 cut-over, weeks 6-8 steady state. Python 3 standard library only. |
| `data/warehouse_tasks_ORDIM_C.csv` | 24k confirmed pick tasks in the shape of /SCWM/ORDIM_C, with travel metres and labour seconds per task. |
| `data/bins_LAGP.csv` | Storage bins with storage type and coordinates, as in /SCWM/LAGP. |
| `data/products.csv` | Products with ABC class, bin before and bin after slotting. |
| `data/daily_kpis.csv` | Per working day: warehouse orders, open tasks over 4 h, dock-to-stock hours. |
| `data/aggregates.json` | All dashboard figures in one file. |
| `data/dash/*.csv` | The aggregates split per dashboard dataset. |
| `dashboard/index.html` | The dashboard page (d3 v7, written for the Claude Dashboard artifact type; reads the CSVs above as attached datasets). |

## Sample result (steady state vs baseline)

| KPI | Before | After |
| --- | --- | --- |
| Travel per pick line | 70.2 m | 38.6 m |
| Labour seconds per pick line (incl. 18% indirect) | 138.6 | 82.9 |
| Picks per labour hour | 26.0 | 43.4 |
| Bin denial rate | 2.6% | 0.9% |
| Picks from high rack above level 1 | 35% | 4% |

The effect is large because the simulated baseline slotting is poor. Plan on 40 to 60% of it
for a site that already has some pick-face discipline.

## Reproduce

```
cd sap-ewm-demo
python3 generate_sample.py
```

# Refactoring notes (reference)

**Start here instead:** [SAFE_SHIP_GUIDE.md](./SAFE_SHIP_GUIDE.md)

That guide is the practical plan: small stages, one check after each, low risk of breakage.

This file is only a **short reference** of what we found. Do not treat it as “do everything at once.”

---

## What the tool is

Measure AHV host / CVM CPU overhead while many UVMs run a workload (redis / fio / dirtyHarry).

Flow: `setupVms.py` once → `overheadTest.py` to measure → CSV + terminal table.

Preferred collector: `bpfsnap` (eBPF). Legacy: `schedstat`.

---

## Must fix before a public/open release

1. Hardcoded VM password (`libx/lib.py`, `setupVms.py`) → env var  
2. Internal URLs (image, redis, harry, bpftrace) → config / env  
3. Silent `exit(1)` / `print(e)` on fatal errors → clear failures  
4. Infinite waits for VM IP → timeouts  

---

## Safe to delete early (no behavior change)

- `ephemeral_monitor.c`, `test_cg_exit.c`  
- `_tmp_parse5s.py`, scratch `cgtop_*`  
- Local experiment rules under `.cursor/`  

## Do not delete until later

- `schedstatCollector.py` — bpfsnap still imports helpers from it  
- Extract metrics first, then archive schedstat  
- `iperfTest.py` — optional; leave until core path is solid  

---

## Recommended sequence

See **SAFE_SHIP_GUIDE.md** Stages 0–6.

Short version:

```text
backup → delete dead files → secrets/URLs → louder errors
     → extract metrics → (optional) drop legacy / tidy dumps
     → README for outsiders
```

Unified CLI and full package rename come **last**, after the tool is boringly reliable.

---

## Success bar for “open”

- No secrets in source  
- README a stranger can follow  
- setup + run still work on a real cluster  
- Bad config fails with a readable message  

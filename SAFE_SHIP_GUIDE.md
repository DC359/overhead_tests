# Safe Ship Guide — overhead-tests

How to turn this lab tool into something others can use **without breaking what already works**.

---

## The rule that prevents breakage

**One change → one check → one commit.**

Never rewrite the whole repo in one pass.

| Do | Don't |
|----|--------|
| Change one concern at a time | "Refactor everything this weekend" |
| Run a real experiment after each step | Assume it still works |
| Commit after each green check | Mix secret-removal + CLI rewrite + deletions |
| Keep old scripts until the new path works | Delete first, fix later |

If something breaks, `git revert` that one commit. You always know what caused it.

---

## How to know it still works

After **every** step, run the smallest real check you can:

```bash
# 1) Syntax / import check (always)
python3 -m py_compile setupVms.py overheadTest.py
python3 -c "from util.ObjFactory import ObjFactory; print('ok')"

# 2) Dry config load
python3 -c "import json; json.load(open('sample/redis.json')); print('config ok')"

# 3) Real cluster smoke (when you have a host)
#    Use a tiny config: few VMs, short baseline, 1 run
./setupVms.py  -i <cvm> -f sample/redis.json -H <host>   # only if setup changed
./overheadTest.py -i <cvm> -f sample/redis.json -H <host>
```

**No cluster access today?**
- Stages 0, 1, 2 are safe without cluster (just syntax checks)
- Stage 3A (password) **requires** cluster to test — defer it
- Document which stages passed local checks, test 3A+3B together when you have a box

---

## The safe order (6 small stages)

Each stage is shippable on its own. Stop anytime; the tool is never “half-dead.”

### Stage 0 — Freeze + backup (30 min)

Goal: a known-good baseline.

```bash
git status
git branch backup/before-ship
# optional: tag
git tag pre-ship-baseline
```

**Check:** branch exists, current code still runs as today.

---

### Stage 1 — Delete only dead files (1–2 hours)

Goal: less noise. **No behavior change.**

Safe to delete (nothing in the main path imports these):

- `ephemeral_monitor.c`
- `test_cg_exit.c`
- `_tmp_parse5s.py`
- `.cursor/rules/modified-experiment.mdc` (local only)
- leftover `cgtop_*` scratch files on disk

**Do not delete yet:**

- `schedstatCollector.py` (bpfsnap still imports from it)
- `schedstat_collect.c` (used by schedstat path)
- `iperfTest.py` / iperf workload (parked on branch `archive/iperf`)

**Check:** `py_compile` + import factory.  
**Commit:** `chore: remove unused prototypes and scratch files`

---

### Stage 2 — Better errors FIRST (half day)

Goal: when something breaks in later stages, you'll see *why*. Do this **before** secrets.

Change **only** error paths:

- Replace bare `exit(1)` in `ObjFactory` with a clear message + exit
- Replace "print and continue" on fatal VM create/clone with raise + stop
- Add timeouts to `Vm.getIp()` / `waitForReady()` (prevent infinite hang)

Do **not** touch secrets or URLs yet.

**Check:** deliberately pass a bad collector name / bad host — should fail with a clear message, not silent crash.  
**Commit:** `fix: clearer errors and timeouts on cluster waits`

---

### Stage 3A — Password only (1 hour)

Goal: no hardcoded password in repo. **Highest-risk change** — do this alone, not mixed with URLs.

Change **only**:

1. `libx/lib.py` line 3: delete `VM_PASSWORD = "nutanix/4u"`, read from `os.environ.get("OVERHEAD_VM_PASSWORD")`
2. `setupVms.py` line 51: change `sshpass -p "nutanix/4u"` to use the env var
3. Fail early if env var not set

Keep everything else unchanged.

```bash
export OVERHEAD_VM_PASSWORD='nutanix/4u'  # or your actual password
```

**Check (no cluster needed):**
```bash
python3 -c "from libx.lib import run_remote_cmd; print('imports ok')"
# then, if you have a cluster:
./setupVms.py -f sample/redis.json -H <host>  # should still work
```

**Commit:** `fix: load VM password from OVERHEAD_VM_PASSWORD env var`

---

### Stage 3B — URLs only (1 hour)

Goal: binary/image URLs configurable. Do **after** password works.

Change **only**:

1. `util/cvm.py` line 131: image URL from config or `OVERHEAD_IMAGE_URL` env
2. `workload/redisWorkload.py`: add `bin_url` to config (optional, with default for now)
3. `workload/dirtyHarry.py`: same
4. `workload/iperfWorkload.py`: parked on `archive/iperf` (restore later if needed)

Or **simpler first pass**: just add env var fallbacks, keep hardcoded URLs as defaults for now. Remove defaults later.

**Check:** with env vars set, one setup run.  
**Commit:** `fix: make binary URLs configurable via config or env`

---

### Stage 5 — Extract shared metrics (half day)

Goal: unlock deleting legacy collector later **without** breaking bpfsnap.

1. Create `statsCollector/metrics.py` (keep it in statsCollector for now)
2. Move `_build_metrics` function (+ any constants it needs) there
3. Change **both** `schedstatCollector.py` and `bpfSnapCollector.py` to import from new file
4. Run one bpfsnap experiment

**Do not delete** `schedstatCollector.py` in this stage. Both collectors should still work.

**Check:** 
```bash
python3 -c "from statsCollector.metrics import _build_metrics; print('ok')"
# then a real bpfsnap run - CSV/table should match previous runs
```

**Commit:** `refactor: extract shared metrics to separate module`

---

### Stage 6 — Optional cleanup (only after Stage 5 is green)

Pick **one** of these per day, not all at once:

| Optional step | When safe |
|---------------|-----------|
| Archive/remove `schedstat` collector + `schedstat_collect.c` | Only if nobody needs fallback |
| Strip `-modified` columns from dumps | After one bpfsnap run looks correct |
| Thin sample configs to 3–4 gold files | Anytime |
| Restore iperf from `archive/iperf` when needed | Later |

**Check after each:** compile + one real run if collector/dumps changed.  
**Commit:** one commit per optional step.

---

### Stage 7 — Docs + packaging (half day)

Goal: strangers understand it.

Update `README.md`:

- What the tool measures (one paragraph)
- Requirements (cluster, Python, sshpass, env vars)
- Two commands: setup then run
- How to read stable-phase `x_cores`

Add later (not required to “work”):

- `LICENSE`
- `pyproject.toml` / install instructions
- Unified `overhead` CLI (only after Stages 1–5 feel solid)

**Check:** ask a teammate to follow README cold.  
**Commit:** `docs: external-friendly README and env requirements`

---

## What *not* to do in the first month

These are high-breakage. Save them until Stages 1–5 are boringly stable:

- Renaming the whole tree to `overhead/`
- Replacing both scripts with a new CLI in one PR
- Rewriting CVM/Host/Vm “properly” while also changing collectors
- Deleting schedstat before metrics extraction
- “While I’m here” drive-by cleanups

---

## Simple decision map

```text
Want safer publish, minimal risk?
  → Stages 0 → 1 → 2 → 3A → 3B → 7
  (stop here: usable outside Nutanix, same mental model)

Want cleaner codebase next?
  → then Stage 5 → Stage 6 (one item at a time)

Want “product” CLI later?
  → only after the above feels routine
```

---

## Branch strategy (keeps main working)

```text
main                    ← always runnable lab tool
  └── ship/stage-1-dead-files
  └── ship/stage-2-errors
  └── ship/stage-3a-password
  └── ship/stage-3b-urls
  └── ship/stage-5-metrics
```

Merge each branch only after its check passes. Never stack five unfinished refactors on one branch.

---

## Definition of “good enough to open”

You can open the repo publicly when:

1. No password / secret in git history going forward (Stage 3A done; rotate if history already leaked)
2. README lists env vars and a working setup/run flow
3. Dead prototypes are gone (Stage 1)
4. Bad config fails with a readable message (Stage 2)
5. At least one recent successful run on a real host after Stage 3A

You do **not** need a perfect package layout or deleted schedstat for that.

---

## If something breaks anyway

1. Note which stage/commit you were on  
2. `git log -5 --oneline`  
3. `git revert <that-commit>` or reset that branch to `main`  
4. Re-do the stage in a smaller chunk  

Never “fix forward” five more unrelated things while the tool is red.

---

## Suggested first day (concrete)

Morning:

1. Stage 0 backup branch  
2. Stage 1 delete dead files + commit  

Afternoon:

3. Stage 2 better errors (no cluster needed)  
4. Smoke test: pass a bad collector name, should fail clearly

**Don't do Stage 3A (password) on the same day** — that needs a real cluster check. Save it for when you have test time.

That's enough progress without risking a malfunctioning tree.  

That’s enough progress without risking a malfunctioning tree.

// schedstat_snap.bpf.c — in-kernel half of the BPF snapshot+exit collector.
//
// Idea: Each thread has cumulative run and scheduler-wait counters for its lifetime
// (run = se.sum_exec_runtime, wait = sched_info.run_delay). We keep, per thread,
// the odometer value we last counted (the "baseline book", map `last`).
// Threads are accounted either during/by:
//   1) the periodic sweep (iter/task) reads every LIVE thread, adds (now - last) to
//      its slice's scoreboard, and refreshes last.
//   2) the exit hook (tp_btf/sched_process_exit) catches DYING threads, adds
//      (final - last), and deletes them from the baseline book.
// Together these account for long-running, newborn, dying and ephemeral threads
// without ever streaming per-thread data to userspace — only the small
// per-slice / per-service scoreboards (agg_slice, agg_svc) cross the boundary.

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "GPL";

// bucket ids for each slice
#define BUCKET_OTHER    0   //If OTHER is unusually large, possible misconfigured cgrp config.
#define BUCKET_CVM      1   // ahv-cvm.slice
#define BUCKET_UVMS     2   // ahv-uvms.slice
#define BUCKET_SERVICES 3   // system.slice (reported as ahv.services)
#define N_SLICE_BUCKETS 4

// system.slice sits at cgroup level 1; ahv-cvm/ahv-uvms sit at level 2 under
// ahv.slice (level 1). These depths are fixed by the AHV cgroup layout. The
// ancestor at a fixed level is a single array read — no parent-walk loop — so a
// thread can be buried arbitrarily deep and still be classified correctly.
#define SYS_LEVEL   1
#define SLICE_LEVEL 2

// Slice cgroup ids, filled in by userspace (via name_to_handle_at) before load.
const volatile __u64 cvm_id  = 0;
const volatile __u64 uvms_id = 0;
const volatile __u64 sys_id  = 0;

struct vals {
    __u64 run;   // se.sum_exec_runtime snapshot
    __u64 wait;  // sched_info.run_delay snapshot
};

struct accum {
    __u64 run;    // summed run delta this round
    __u64 wait;   // summed wait delta this round
    __u64 count;  // number of thread-contributions this round
};

// Baseline book: tid -> last odometer reading we counted.
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 262144);
    __type(key, __u32);
    __type(value, struct vals);
} last SEC(".maps");

// Per-slice scoreboard (4 fixed buckets). Per-CPU to avoid contention 
// between CPUs; userspace sums across CPUs.
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, N_SLICE_BUCKETS);
    __type(key, __u32);
    __type(value, struct accum);
} agg_slice SEC(".maps");

// Per-service scoreboard, keyed by the *.service/*.scope cgroup id directly
// under system.slice. Userspace maps id -> name.
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __uint(max_entries, 8192);
    __type(key, __u64);
    __type(value, struct accum);
} agg_svc SEC(".maps");

static __always_inline void add_slice(__u32 bucket, __u64 run, __u64 wait)
{
    struct accum *a = bpf_map_lookup_elem(&agg_slice, &bucket);
    if (a) {
        a->run += run;
        a->wait += wait;
        a->count += 1;
    }
}

static __always_inline void add_svc(__u64 id, __u64 run, __u64 wait)
{
    struct accum *a = bpf_map_lookup_elem(&agg_svc, &id);
    if (a) {
        a->run += run;
        a->wait += wait;
        a->count += 1;
    } else {
        struct accum n = { .run = run, .wait = wait, .count = 1 };
        bpf_map_update_elem(&agg_svc, &id, &n, BPF_ANY);
    }
}

// Resolve which slice (and, for system.slice, which service) a task belongs to.
// Reads the kernel's ready-made ancestor array at fixed levels; no loop.
static __always_inline void classify(struct task_struct *task,
                                     __u32 *bucket, __u64 *svc_id)
{
    *bucket = BUCKET_OTHER;
    *svc_id = 0;

    struct cgroup *cg = BPF_CORE_READ(task, cgroups, dfl_cgrp);
    if (!cg)
        return;

    int level = BPF_CORE_READ(cg, level);

    __u64 id_sys = 0;     // ancestor id at SYS_LEVEL
    __u64 id_slice = 0;   // ancestor id at SLICE_LEVEL

    if (level >= SYS_LEVEL)
        id_sys = BPF_CORE_READ(cg, ancestors[SYS_LEVEL], kn, id);
    if (level >= SLICE_LEVEL)
        id_slice = BPF_CORE_READ(cg, ancestors[SLICE_LEVEL], kn, id);

    if (sys_id != 0 && id_sys == sys_id) {
        // Under system.slice -> ahv.services bucket; service is the child one
        // level deeper (the *.service / *.scope). If the thread sits directly
        // in system.slice, use system.slice's own id.
        *bucket = BUCKET_SERVICES;
        if (level >= SLICE_LEVEL)
            *svc_id = id_slice;
        else
            *svc_id = id_sys;
        return;
    }

    if (cvm_id != 0 && id_slice == cvm_id) {
        *bucket = BUCKET_CVM;
        return;
    }
    if (uvms_id != 0 && id_slice == uvms_id) {
        *bucket = BUCKET_UVMS;
        return;
    }
}

// Core accounting for one task. is_exit selects refresh-vs-delete of baseline.
static __always_inline void account(struct task_struct *task, int is_exit)
{
    __u32 tid = BPF_CORE_READ(task, pid);  // kernel 'pid' == userspace TID
    if (tid == 0)
        return;  // idle/swapper

    __u64 run  = BPF_CORE_READ(task, se.sum_exec_runtime);
    __u64 wait = BPF_CORE_READ(task, sched_info.run_delay);

    struct vals *prev = bpf_map_lookup_elem(&last, &tid);
    __u64 drun, dwait;
    if (prev && run >= prev->run) {
        drun  = run - prev->run;
        dwait = (wait >= prev->wait) ? (wait - prev->wait) : wait;
    } else {
        // No usable baseline: count the task's current lifetime totals
        drun  = run;
        dwait = wait;
    }

    __u32 bucket;
    __u64 svc_id;
    classify(task, &bucket, &svc_id);

    add_slice(bucket, drun, dwait);
    if (bucket == BUCKET_SERVICES && svc_id != 0)
        add_svc(svc_id, drun, dwait);

    if (is_exit) {
        bpf_map_delete_elem(&last, &tid);
    } else {
        struct vals nv = { .run = run, .wait = wait };
        bpf_map_update_elem(&last, &tid, &nv, BPF_ANY);
    }
}

// Userspace-triggered periodic sweep over all currently live tasks
SEC("iter/task")
int snap_iter(struct bpf_iter__task *ctx)
{
    struct task_struct *task = ctx->task;
    if (!task)
        return 0;
    account(task, 0);
    return 0;
}

// Account a thread's final delta when it exits, then remove its baseline.
SEC("tp_btf/sched_process_exit")
int BPF_PROG(snap_exit, struct task_struct *p)
{
    if (p)
        account(p, 1);
    return 0;
}

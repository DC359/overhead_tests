// SPDX-License-Identifier: GPL-2.0
//
// schedstat_snap.c — userspace driver for the BPF snapshot+exit collector.
//
// Usage:
//   schedstat_snap <interval_s> <outfile> <pidfile> \
//                  <cvm_cg_path> <uvms_cg_path> <sys_cg_path> <service_parent>
//
// Every <interval_s> seconds it:
//   1) triggers the iter/task sweep (kernel adds each live thread's delta),
//   2) reads + sums the per-CPU scoreboards (agg_slice, agg_svc),
//   3) resolves service cgroup ids -> names by scanning <service_parent>,
//   4) writes one TICK block to <outfile> in the aggregated format the Python
//      collector parses, then
//   5) wipes the scoreboards for the next round.
//
// All per-thread accounting happens in the kernel; only the small scoreboards
// cross to userspace.

#ifndef _GNU_SOURCE
#define _GNU_SOURCE  // name_to_handle_at, struct file_handle, localtime_r
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <signal.h>
#include <errno.h>
#include <dirent.h>
#include <stdint.h>
#include <sys/syscall.h>
#include <linux/types.h>

#include <bpf/libbpf.h>
#include <bpf/bpf.h>

#include "schedstat_snap.skel.h"

// name_to_handle_at file handle wrapper big enough for any fs handle.
struct cg_handle {
    struct file_handle fh;
    unsigned char data[128];
};

#define BUCKET_OTHER    0
#define BUCKET_CVM      1
#define BUCKET_UVMS     2
#define BUCKET_SERVICES 3
#define N_SLICE_BUCKETS 4

static const char *SLICE_NAMES[N_SLICE_BUCKETS] = {
    "other", "ahv-cvm.slice", "ahv-uvms.slice", "ahv.services",
};

struct accum {
    __u64 run;
    __u64 wait;
    __u64 count;
};

static volatile sig_atomic_t exiting = 0;

static void on_signal(int sig)
{
    (void)sig;
    exiting = 1;
}

// Return the 64-bit cgroup id for a cgroupfs path, matching the kernel's
// kn->id / bpf_get_current_cgroup_id(). The first 8 bytes of the file handle
// encode that id for cgroupfs.
static __u64 cgid_from_path(const char *path)
{
    struct cg_handle h;
    int mount_id = 0;
    h.fh.handle_bytes = sizeof(h.data);
    if (name_to_handle_at(AT_FDCWD, path, &h.fh, &mount_id, 0) != 0)
        return 0;
    __u64 id = 0;
    size_t n = h.fh.handle_bytes < sizeof(id) ? h.fh.handle_bytes : sizeof(id);
    memcpy(&id, h.fh.f_handle, n);
    return id;
}

// Build a fresh id->name table for the children of <parent> (the *.service /
// *.scope / *.slice cgroups directly under system.slice). Re-scanned each tick
// so dynamically created VM/service scopes are always named correctly.
struct svc_name {
    __u64 id;
    char name[128];
};

static int scan_services(const char *parent, struct svc_name *out, int max)
{
    DIR *d = opendir(parent);
    if (!d)
        return 0;
    struct dirent *e;
    int n = 0;
    char childpath[512];
    while ((e = readdir(d)) != NULL && n < max) {
        if (e->d_name[0] == '.')
            continue;
        if (e->d_type != DT_DIR && e->d_type != DT_UNKNOWN)
            continue;
        snprintf(childpath, sizeof(childpath), "%s/%s", parent, e->d_name);
        __u64 id = cgid_from_path(childpath);
        if (id == 0)
            continue;
        out[n].id = id;
        snprintf(out[n].name, sizeof(out[n].name), "%s", e->d_name);
        n++;
    }
    closedir(d);
    return n;
}

static const char *name_for_id(struct svc_name *tbl, int n, __u64 id, char *buf, size_t buflen)
{
    for (int i = 0; i < n; i++) {
        if (tbl[i].id == id)
            return tbl[i].name;
    }
    snprintf(buf, buflen, "svc-%llu", (unsigned long long)id);
    return buf;
}

// Run the iterator to EOF so the kernel program executes over every task.
static int trigger_sweep(struct bpf_link *iter_link)
{
    int iter_fd = bpf_iter_create(bpf_link__fd(iter_link));
    if (iter_fd < 0) {
        fprintf(stderr, "bpf_iter_create failed: %d\n", iter_fd);
        return -1;
    }
    char buf[4096];
    ssize_t r;
    while ((r = read(iter_fd, buf, sizeof(buf))) > 0)
        ;
    int err = (r < 0) ? -errno : 0;
    close(iter_fd);
    return err;
}

int main(int argc, char **argv)
{
    if (argc < 8) {
        fprintf(stderr,
                "usage: %s <interval_s> <outfile> <pidfile> "
                "<cvm_cg> <uvms_cg> <sys_cg> <service_parent>\n",
                argv[0]);
        return 1;
    }

    int interval_s        = atoi(argv[1]);
    const char *outpath   = argv[2];
    const char *pidpath   = argv[3];
    const char *cvm_cg    = argv[4];
    const char *uvms_cg   = argv[5];
    const char *sys_cg    = argv[6];
    const char *svc_parent= argv[7];

    if (interval_s <= 0)
        interval_s = 5;

    int ncpu = libbpf_num_possible_cpus();
    if (ncpu <= 0)
        ncpu = 1;

    __u64 cvm_id  = cgid_from_path(cvm_cg);
    __u64 uvms_id = cgid_from_path(uvms_cg);
    __u64 sys_id  = cgid_from_path(sys_cg);
    fprintf(stderr, "[snap] slice ids: cvm=%llu uvms=%llu sys=%llu (ncpu=%d)\n",
            (unsigned long long)cvm_id, (unsigned long long)uvms_id,
            (unsigned long long)sys_id, ncpu);
    if (sys_id == 0)
        fprintf(stderr, "[snap] WARNING: system.slice id resolved to 0 — "
                        "service accounting will be empty\n");

    struct schedstat_snap_bpf *skel = schedstat_snap_bpf__open();
    if (!skel) {
        fprintf(stderr, "failed to open skeleton\n");
        return 1;
    }

    skel->rodata->cvm_id  = cvm_id;
    skel->rodata->uvms_id = uvms_id;
    skel->rodata->sys_id  = sys_id;

    if (schedstat_snap_bpf__load(skel)) {
        fprintf(stderr, "failed to load BPF skeleton\n");
        schedstat_snap_bpf__destroy(skel);
        return 1;
    }

    // Attach the exit tracepoint.
    struct bpf_link *exit_link = bpf_program__attach(skel->progs.snap_exit);
    if (!exit_link) {
        fprintf(stderr, "failed to attach snap_exit\n");
        schedstat_snap_bpf__destroy(skel);
        return 1;
    }

    // Attach the task iterator (link is reused; each tick we create an fd).
    struct bpf_link *iter_link = bpf_program__attach_iter(skel->progs.snap_iter, NULL);
    if (!iter_link) {
        fprintf(stderr, "failed to attach snap_iter\n");
        bpf_link__destroy(exit_link);
        schedstat_snap_bpf__destroy(skel);
        return 1;
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    // Write pid file so the Python driver can stop us.
    FILE *pf = fopen(pidpath, "w");
    if (pf) {
        fprintf(pf, "%d\n", getpid());
        fclose(pf);
    }

    int slice_fd = bpf_map__fd(skel->maps.agg_slice);
    int svc_fd   = bpf_map__fd(skel->maps.agg_svc);

    struct accum *percpu = calloc(ncpu, sizeof(struct accum));
    struct accum *zero   = calloc(ncpu, sizeof(struct accum));
    struct svc_name *svctbl = calloc(8192, sizeof(struct svc_name));
    if (!percpu || !zero || !svctbl) {
        fprintf(stderr, "alloc failed\n");
        return 1;
    }

    // Truncate the output file once per process launch so a new run never
    // inherits a previous run's ticks. This is the authoritative reset and is
    // resilient to the Python-side `rm` failing (e.g. an SSH ControlMaster
    // hiccup returning exit 255). Per-tick writes below still use append mode.
    {
        FILE *trunc = fopen(outpath, "w");
        if (trunc)
            fclose(trunc);
        else
            fprintf(stderr, "[snap] WARNING: could not truncate %s: %s\n",
                    outpath, strerror(errno));
    }

    // Prime the baseline: a first sweep records every live thread's odometer
    // without it being attributed to a displayed tick.
    trigger_sweep(iter_link);
    // Clear whatever that priming sweep accumulated.
    for (__u32 k = 0; k < N_SLICE_BUCKETS; k++)
        bpf_map_update_elem(slice_fd, &k, zero, BPF_ANY);
    {
        __u64 key = 0, next;
        while (bpf_map_get_next_key(svc_fd, &key, &next) == 0) {
            bpf_map_delete_elem(svc_fd, &next);
            key = next;
        }
    }

    unsigned long tick = 0;
    struct timespec ts_mono;

    while (!exiting) {
        // Sleep the interval in 1s chunks so SIGTERM is responsive.
        for (int s = 0; s < interval_s && !exiting; s++)
            sleep(1);
        if (exiting)
            break;

        tick++;

        // Wall-clock + monotonic timestamps for this tick.
        time_t now = time(NULL);
        struct tm tmv;
        localtime_r(&now, &tmv);
        char ts_start[32];
        strftime(ts_start, sizeof(ts_start), "%H:%M:%S", &tmv);
        clock_gettime(CLOCK_MONOTONIC, &ts_mono);
        unsigned long long mono_ns =
            (unsigned long long)ts_mono.tv_sec * 1000000000ULL + ts_mono.tv_nsec;

        // 1) sweep all live threads.
        trigger_sweep(iter_link);

        // 2) read + sum the per-slice scoreboard.
        struct accum slice_tot[N_SLICE_BUCKETS];
        memset(slice_tot, 0, sizeof(slice_tot));
        for (__u32 k = 0; k < N_SLICE_BUCKETS; k++) {
            if (bpf_map_lookup_elem(slice_fd, &k, percpu) != 0)
                continue;
            for (int c = 0; c < ncpu; c++) {
                slice_tot[k].run   += percpu[c].run;
                slice_tot[k].wait  += percpu[c].wait;
                slice_tot[k].count += percpu[c].count;
            }
        }

        // 3) read + sum the per-service scoreboard, resolve names.
        int nsvc = scan_services(svc_parent, svctbl, 8192);

        time_t now2 = time(NULL);
        localtime_r(&now2, &tmv);
        char ts_end[32];
        strftime(ts_end, sizeof(ts_end), "%H:%M:%S", &tmv);

        FILE *out = fopen(outpath, "a");
        if (!out) {
            fprintf(stderr, "cannot open %s: %s\n", outpath, strerror(errno));
            break;
        }

        fprintf(out, "TICK %lu %s %s %d MONO=%lluns\n",
                tick, ts_start, ts_end, interval_s, mono_ns);

        for (__u32 k = 0; k < N_SLICE_BUCKETS; k++) {
            fprintf(out, "SLICE %s %llu %llu %llu\n",
                    SLICE_NAMES[k],
                    (unsigned long long)slice_tot[k].run,
                    (unsigned long long)slice_tot[k].wait,
                    (unsigned long long)slice_tot[k].count);
        }

        {
            char namebuf[64];
            __u64 key = 0, next;
            int have_key = (bpf_map_get_next_key(svc_fd, NULL, &next) == 0);
            while (have_key) {
                if (bpf_map_lookup_elem(svc_fd, &next, percpu) == 0) {
                    struct accum t = {0, 0, 0};
                    for (int c = 0; c < ncpu; c++) {
                        t.run   += percpu[c].run;
                        t.wait  += percpu[c].wait;
                        t.count += percpu[c].count;
                    }
                    const char *nm = name_for_id(svctbl, nsvc, next,
                                                 namebuf, sizeof(namebuf));
                    fprintf(out, "SERVICE %s %llu %llu %llu\n",
                            nm,
                            (unsigned long long)t.run,
                            (unsigned long long)t.wait,
                            (unsigned long long)t.count);
                }
                key = next;
                have_key = (bpf_map_get_next_key(svc_fd, &key, &next) == 0);
            }
        }

        fprintf(out, "END_TICK\n");
        fclose(out);

        // 5) wipe the scoreboards for the next round.
        for (__u32 k = 0; k < N_SLICE_BUCKETS; k++)
            bpf_map_update_elem(slice_fd, &k, zero, BPF_ANY);
        {
            __u64 key = 0, next;
            // Collect-then-delete to avoid mutating during iteration.
            static __u64 keys[8192];
            int nk = 0;
            int have = (bpf_map_get_next_key(svc_fd, NULL, &next) == 0);
            while (have && nk < 8192) {
                keys[nk++] = next;
                key = next;
                have = (bpf_map_get_next_key(svc_fd, &key, &next) == 0);
            }
            for (int i = 0; i < nk; i++)
                bpf_map_delete_elem(svc_fd, &keys[i]);
        }
    }

    fprintf(stderr, "[snap] exiting, wrote %lu ticks\n", tick);
    unlink(pidpath);
    free(percpu);
    free(zero);
    free(svctbl);
    bpf_link__destroy(iter_link);
    bpf_link__destroy(exit_link);
    schedstat_snap_bpf__destroy(skel);
    return 0;
}

/*
 * schedstat_collect.c — Fast schedstat collector for AHV overhead measurement.
 *
 * Replaces the bash collection script. Reads per-TID schedstat and cpu.stat
 * from cgroup trees, outputs in the same text format the Python parser expects.
 *
 * Usage:
 *   schedstat_collect <interval_ms> <outfile> <pid_file> \
 *       <slice_path0> <slice_name0> <slice_path1> <slice_name1> <slice_path2> <slice_name2> \
 *       <service_parent_path>
 *
 * Output format (per tick, appended to outfile):
 *   TICK <n> <HH:MM:SS> <HH:MM:SS_end> <delta_s> ELAPSED=<ms>ms SLEEP=<ms>ms MONO=<ns>
 *   SLICE <name> <tid> <run_ns> <wait_ns>
 *   SERVICE <svc.service> <tid> <run_ns> <wait_ns>
 *   CPUSTAT <slice_name> <usage_usec>
 *   SVC_CPUSTAT <svc.service> <usage_usec>
 *   TIMING slice_enum <ms>ms svc_enum <ms>ms cpustat <ms>ms total <ms>ms
 *   END_TICK
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>
#include <unistd.h>
#include <time.h>
#include <signal.h>
#include <errno.h>
#include <sys/types.h>

#define MAX_TIDS     8192
#define MAX_PATH     512
#define MAX_SERVICES 64
#define MAX_SVC_NAME 128

static const char *VERIFY_SERVICES[] = {
    "libvirtd.service",
    "ovs-vswitchd.service",
    "ahv-host-agent.service",
    "ahv-storaged.service",
    "NetworkManager.service",
    "vhostmd.service",
    NULL
};

static volatile int g_running = 1;

static void sig_handler(int sig) {
    (void)sig;
    g_running = 0;
}

static long long ms_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static long long ns_mono_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void sleep_until_mono(const struct timespec *target) {
    struct timespec now, rem;
    clock_gettime(CLOCK_MONOTONIC, &now);
    long long diff_ns = ((long long)target->tv_sec - now.tv_sec) * 1000000000LL
                      + (target->tv_nsec - now.tv_nsec);
    if (diff_ns <= 0) return;
    rem.tv_sec  = diff_ns / 1000000000LL;
    rem.tv_nsec = diff_ns % 1000000000LL;
    while (nanosleep(&rem, &rem) == -1 && errno == EINTR)
        ;
}

static void get_wall_clock(char *buf, size_t len) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tm;
    localtime_r(&ts.tv_sec, &tm);
    snprintf(buf, len, "%02d:%02d:%02d", tm.tm_hour, tm.tm_min, tm.tm_sec);
}

static int tid_list[MAX_TIDS];
static int tid_count;

static void collect_pids_recursive(const char *cg_dir) {
    char path[MAX_PATH];
    snprintf(path, sizeof(path), "%s/cgroup.procs", cg_dir);

    FILE *f = fopen(path, "r");
    if (f) {
        int pid;
        while (fscanf(f, "%d", &pid) == 1) {
            if (tid_count < MAX_TIDS)
                tid_list[tid_count++] = pid;
        }
        fclose(f);
    }

    DIR *d = opendir(cg_dir);
    if (!d) return;
    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        if (ent->d_name[0] == '.') continue;
        if (ent->d_type != DT_DIR) continue;
        char subdir[MAX_PATH];
        snprintf(subdir, sizeof(subdir), "%s/%s", cg_dir, ent->d_name);
        collect_pids_recursive(subdir);
    }
    closedir(d);
}

static void expand_pids_to_tids(void) {
    int orig_count = tid_count;
    int new_tids[MAX_TIDS];
    int new_count = 0;

    for (int i = 0; i < orig_count; i++) {
        char task_dir[MAX_PATH];
        snprintf(task_dir, sizeof(task_dir), "/proc/%d/task", tid_list[i]);
        DIR *d = opendir(task_dir);
        if (!d) continue;
        struct dirent *ent;
        while ((ent = readdir(d)) != NULL) {
            if (ent->d_name[0] == '.') continue;
            int tid = atoi(ent->d_name);
            if (tid > 0 && new_count < MAX_TIDS)
                new_tids[new_count++] = tid;
        }
        closedir(d);
    }

    memcpy(tid_list, new_tids, new_count * sizeof(int));
    tid_count = new_count;
}

static void read_and_print_schedstat(FILE *out, const char *tag, const char *name) {
    char path[MAX_PATH];
    unsigned long run, wait;

    for (int i = 0; i < tid_count; i++) {
        snprintf(path, sizeof(path), "/proc/%d/schedstat", tid_list[i]);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        if (fscanf(f, "%lu %lu", &run, &wait) == 2) {
            fprintf(out, "%s %s %d %lu %lu\n", tag, name, tid_list[i], run, wait);
        }
        fclose(f);
    }
}

static void collect_slice(FILE *out, const char *slice_path, const char *slice_name) {
    tid_count = 0;
    collect_pids_recursive(slice_path);
    expand_pids_to_tids();
    read_and_print_schedstat(out, "SLICE", slice_name);
}

static void collect_services(FILE *out, const char *svc_parent) {
    DIR *d = opendir(svc_parent);
    if (!d) return;
    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        if (ent->d_type != DT_DIR) continue;
        size_t nlen = strlen(ent->d_name);
        if (nlen < 9 || strcmp(ent->d_name + nlen - 8, ".service") != 0)
            continue;

        char svc_dir[MAX_PATH];
        snprintf(svc_dir, sizeof(svc_dir), "%s/%s", svc_parent, ent->d_name);

        tid_count = 0;
        collect_pids_recursive(svc_dir);
        expand_pids_to_tids();
        read_and_print_schedstat(out, "SERVICE", ent->d_name);
    }
    closedir(d);
}

static long long read_usage_usec(const char *cpustat_path) {
    FILE *f = fopen(cpustat_path, "r");
    if (!f) return -1;
    char line[256];
    long long usec = -1;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "usage_usec ", 11) == 0) {
            usec = atoll(line + 11);
            break;
        }
    }
    fclose(f);
    return usec;
}

static void collect_cpustat(FILE *out,
                            const char *slice_paths[], const char *slice_names[], int n_slices,
                            const char *svc_parent) {
    char path[MAX_PATH];

    for (int i = 0; i < n_slices; i++) {
        snprintf(path, sizeof(path), "%s/cpu.stat", slice_paths[i]);
        long long usec = read_usage_usec(path);
        if (usec >= 0)
            fprintf(out, "CPUSTAT %s %lld\n", slice_names[i], usec);
    }

    for (int i = 0; VERIFY_SERVICES[i]; i++) {
        snprintf(path, sizeof(path), "%s/%s/cpu.stat", svc_parent, VERIFY_SERVICES[i]);
        long long usec = read_usage_usec(path);
        if (usec >= 0)
            fprintf(out, "SVC_CPUSTAT %s %lld\n", VERIFY_SERVICES[i], usec);
    }
}

int main(int argc, char *argv[]) {
    if (argc < 11) {
        fprintf(stderr, "Usage: %s <interval_ms> <outfile> <pid_file> "
                "<sp0> <sn0> <sp1> <sn1> <sp2> <sn2> <svc_parent>\n", argv[0]);
        return 1;
    }

    int interval_ms = atoi(argv[1]);
    const char *outfile = argv[2];
    const char *pid_file = argv[3];
    const char *slice_paths[3] = { argv[4], argv[6], argv[8] };
    const char *slice_names[3] = { argv[5], argv[7], argv[9] };
    const char *svc_parent = argv[10];

    signal(SIGTERM, sig_handler);
    signal(SIGINT, sig_handler);

    /* Write PID file */
    FILE *pf = fopen(pid_file, "w");
    if (pf) {
        fprintf(pf, "%d\n", getpid());
        fclose(pf);
    }

    /* Seed collection (discard output) */
    {
        FILE *devnull = fopen("/dev/null", "w");
        if (devnull) {
            for (int i = 0; i < 3; i++)
                collect_slice(devnull, slice_paths[i], slice_names[i]);
            collect_services(devnull, svc_parent);
            fclose(devnull);
        }
    }

    /* Compute next wakeup aligned to wall clock */
    struct timespec next_wake;
    clock_gettime(CLOCK_MONOTONIC, &next_wake);
    next_wake.tv_sec += interval_ms / 1000;
    next_wake.tv_nsec += (interval_ms % 1000) * 1000000L;
    if (next_wake.tv_nsec >= 1000000000L) {
        next_wake.tv_sec++;
        next_wake.tv_nsec -= 1000000000L;
    }

    /* Truncate output file so stale data from a prior run is discarded */
    FILE *trunc = fopen(outfile, "w");
    if (trunc) fclose(trunc);

    int tick = 0;
    time_t epoch_prev = 0;

    /* Wait for first interval */
    sleep_until_mono(&next_wake);

    while (g_running) {
        long long t_loop_start = ms_now();
        long long mono_tick_ns = ns_mono_now();
        tick++;

        char ts_start[16], ts_end[16];
        get_wall_clock(ts_start, sizeof(ts_start));

        time_t epoch_now = time(NULL);
        int delta = (epoch_prev > 0) ? (int)(epoch_now - epoch_prev) : 0;
        epoch_prev = epoch_now;

        /* Collect into a memory buffer (tmpfile) then append to outfile */
        FILE *body = tmpfile();
        if (!body) break;

        long long t0 = ms_now();

        for (int i = 0; i < 3; i++)
            collect_slice(body, slice_paths[i], slice_names[i]);

        long long t1 = ms_now();

        collect_services(body, svc_parent);

        long long t2 = ms_now();

        collect_cpustat(body, slice_paths, slice_names, 3, svc_parent);

        long long t3 = ms_now();

        fprintf(body, "TIMING slice_enum %lldms svc_enum %lldms cpustat %lldms total %lldms\n",
                t1 - t0, t2 - t1, t3 - t2, t3 - t0);

        get_wall_clock(ts_end, sizeof(ts_end));

        long long t_loop_end = ms_now();
        long long elapsed_ms = t_loop_end - t_loop_start;
        long long sleep_ms = interval_ms - elapsed_ms;

        /* Append to output file */
        FILE *out = fopen(outfile, "a");
        if (out) {
            fprintf(out, "TICK %d %s %s %d ELAPSED=%lldms SLEEP=%lldms MONO=%lldns\n",
                    tick, ts_start, ts_end, delta, elapsed_ms, sleep_ms, mono_tick_ns);
            /* Copy body to output */
            fseek(body, 0, SEEK_SET);
            char buf[4096];
            size_t n;
            while ((n = fread(buf, 1, sizeof(buf), body)) > 0)
                fwrite(buf, 1, n, out);
            fprintf(out, "END_TICK\n");
            fclose(out);
        }
        fclose(body);

        /* Schedule next wakeup */
        next_wake.tv_sec += interval_ms / 1000;
        next_wake.tv_nsec += (interval_ms % 1000) * 1000000L;
        if (next_wake.tv_nsec >= 1000000000L) {
            next_wake.tv_sec++;
            next_wake.tv_nsec -= 1000000000L;
        }

        if (g_running && sleep_ms > 0)
            sleep_until_mono(&next_wake);
    }

    return 0;
}

/*
 * ephemeral_monitor.c — Capture CPU time for exiting threads via taskstats,
 * with cgroup/service identification by reading /proc at exit time.
 *
 * Strategy for cgroup identification (in order):
 *   1. Read /proc/<pid>/cgroup — works if thread is still partially alive
 *   2. Read /proc/<pid>/status to get PPid, then /proc/<ppid>/cgroup
 *      — the parent (e.g. vhostmd) is almost always still alive
 *   3. Fall back to "unknown"
 *
 * Output format (one line per exiting thread, appended to outfile):
 *   HH:MM:SS.mmm <pid> <comm> <service_or_unknown> <cpu_run_ns> <cpu_delay_ns>
 *
 * Usage:
 *   ephemeral_monitor <outfile> <pidfile> <cpumask>
 *
 * Compile:
 *   gcc -O2 -o ephemeral_monitor ephemeral_monitor.c
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <time.h>
#include <sys/types.h>
#include <sys/socket.h>

#include <linux/netlink.h>
#include <linux/genetlink.h>
#include <linux/taskstats.h>

/* ---- Read cgroup service for a PID ---- */

static int read_service(int pid, char *buf, size_t buflen) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/cgroup", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;

    char line[512];
    int found = 0;
    while (fgets(line, sizeof(line), f)) {
        char *p = strstr(line, "/system.slice/");
        if (p) {
            p += 14; /* strlen("/system.slice/") */
            char *nl = strchr(p, '\n');
            if (nl) *nl = '\0';
            char *slash = strchr(p, '/');
            if (slash) *slash = '\0';
            if (strstr(p, ".service")) {
                strncpy(buf, p, buflen - 1);
                buf[buflen - 1] = '\0';
                found = 1;
                break;
            }
        }
    }
    fclose(f);
    return found ? 0 : -1;
}

/* Read PPid from /proc/<pid>/status */
static int read_ppid(int pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/status", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;

    char line[256];
    int ppid = -1;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "PPid:\t", 6) == 0) {
            ppid = atoi(line + 6);
            break;
        }
    }
    fclose(f);
    return ppid;
}

/*
 * Try to identify the service for a dying thread:
 *   1. /proc/<pid>/cgroup (thread may still be partially alive)
 *   2. /proc/<ppid>/cgroup via /proc/<pid>/status (parent is likely alive)
 */
static int identify_service(int pid, char *buf, size_t buflen) {
    if (read_service(pid, buf, buflen) == 0)
        return 0;

    int ppid = read_ppid(pid);
    if (ppid > 1 && read_service(ppid, buf, buflen) == 0)
        return 0;

    return -1;
}

/* ---- Netlink helpers (adapted from kernel's getdelays.c) ---- */

#define GENLMSG_DATA(glh) ((void *)(NLMSG_DATA(glh) + GENL_HDRLEN))
#define GENLMSG_PAYLOAD(glh) (NLMSG_PAYLOAD(glh, 0) - GENL_HDRLEN)
#define NLA_DATA(na) ((void *)((char *)(na) + NLA_HDRLEN))
#define NLA_PAYLOAD(len) ((len) - NLA_HDRLEN)

#define MAX_MSG_SIZE 1024

struct msgtemplate {
    struct nlmsghdr n;
    struct genlmsghdr g;
    char buf[MAX_MSG_SIZE];
};

static int create_taskstats_socket(void) {
    int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_GENERIC);
    if (fd < 0) return -1;

    int bufsz = 1024 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bufsz, sizeof(bufsz));

    struct sockaddr_nl local;
    memset(&local, 0, sizeof(local));
    local.nl_family = AF_NETLINK;
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static int send_cmd(int sd, __u16 nlmsg_type, __u32 nlmsg_pid,
                    __u8 genl_cmd, __u16 nla_type,
                    void *nla_data, int nla_len) {
    struct msgtemplate msg;
    struct nlattr *na;
    struct sockaddr_nl nladdr;
    int r, buflen;
    char *buf;

    memset(&msg, 0, sizeof(msg));
    msg.n.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
    msg.n.nlmsg_type = nlmsg_type;
    msg.n.nlmsg_flags = NLM_F_REQUEST;
    msg.n.nlmsg_seq = 0;
    msg.n.nlmsg_pid = nlmsg_pid;
    msg.g.cmd = genl_cmd;
    msg.g.version = 0x1;

    na = (struct nlattr *)GENLMSG_DATA(&msg);
    na->nla_type = nla_type;
    na->nla_len = nla_len + NLA_HDRLEN;
    memcpy(NLA_DATA(na), nla_data, nla_len);
    msg.n.nlmsg_len += NLMSG_ALIGN(na->nla_len);

    buf = (char *)&msg;
    buflen = msg.n.nlmsg_len;
    memset(&nladdr, 0, sizeof(nladdr));
    nladdr.nl_family = AF_NETLINK;

    while ((r = sendto(sd, buf, buflen, 0,
                       (struct sockaddr *)&nladdr, sizeof(nladdr))) < buflen) {
        if (r > 0) { buf += r; buflen -= r; }
        else if (errno != EAGAIN) return -1;
    }
    return 0;
}

static __u16 get_family_id(int sd) {
    struct {
        struct nlmsghdr n;
        struct genlmsghdr g;
        char buf[256];
    } ans;
    char name[32];
    strcpy(name, TASKSTATS_GENL_NAME);

    if (send_cmd(sd, GENL_ID_CTRL, getpid(), CTRL_CMD_GETFAMILY,
                 CTRL_ATTR_FAMILY_NAME, name, strlen(name) + 1) < 0)
        return 0;

    int rep_len = recv(sd, &ans, sizeof(ans), 0);
    if (ans.n.nlmsg_type == NLMSG_ERROR || rep_len < 0 ||
        !NLMSG_OK((&ans.n), rep_len))
        return 0;

    struct nlattr *na = (struct nlattr *)GENLMSG_DATA(&ans);
    na = (struct nlattr *)((char *)na + NLA_ALIGN(na->nla_len));
    if (na->nla_type == CTRL_ATTR_FAMILY_ID)
        return *(__u16 *)NLA_DATA(na);
    return 0;
}

/* ---- Wall clock ---- */

static void get_wall_clock_ms(char *buf, size_t len) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tm;
    localtime_r(&ts.tv_sec, &tm);
    int ms = (int)(ts.tv_nsec / 1000000);
    snprintf(buf, len, "%02d:%02d:%02d.%03d",
             tm.tm_hour, tm.tm_min, tm.tm_sec, ms);
}

/* ---- Main ---- */

static volatile int g_running = 1;
static void sig_handler(int sig) { (void)sig; g_running = 0; }

int main(int argc, char *argv[]) {
    if (argc < 4) {
        fprintf(stderr, "Usage: %s <outfile> <pidfile> <cpumask>\n", argv[0]);
        return 1;
    }

    const char *outfile = argv[1];
    const char *pidfile = argv[2];
    const char *cpumask = argv[3];

    signal(SIGTERM, sig_handler);
    signal(SIGINT, sig_handler);

    FILE *pf = fopen(pidfile, "w");
    if (pf) { fprintf(pf, "%d\n", getpid()); fclose(pf); }

    FILE *out = fopen(outfile, "a");
    if (!out) { perror("fopen outfile"); return 1; }

    int task_fd = create_taskstats_socket();
    if (task_fd < 0) {
        fprintf(stderr, "Failed to create taskstats socket\n");
        fclose(out); return 1;
    }

    __u16 family_id = get_family_id(task_fd);
    if (!family_id) {
        fprintf(stderr, "Failed to get taskstats family id\n");
        close(task_fd); fclose(out); return 1;
    }

    char mask_buf[256];
    strncpy(mask_buf, cpumask, sizeof(mask_buf) - 1);
    mask_buf[sizeof(mask_buf) - 1] = '\0';
    if (send_cmd(task_fd, family_id, getpid(), TASKSTATS_CMD_GET,
                 TASKSTATS_CMD_ATTR_REGISTER_CPUMASK,
                 mask_buf, strlen(mask_buf) + 1) < 0) {
        fprintf(stderr, "Failed to register cpumask '%s'\n", cpumask);
        close(task_fd); fclose(out); return 1;
    }

    fprintf(stderr, "ephemeral_monitor: started (pid=%d, cpumask=%s)\n",
            getpid(), cpumask);

    struct msgtemplate task_msg;
    char ts_buf[32];
    unsigned long long exit_count = 0, found_count = 0;
    int flush_counter = 0;

    while (g_running) {
        int rc = recv(task_fd, &task_msg, sizeof(task_msg), 0);
        if (rc < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (task_msg.n.nlmsg_type == NLMSG_ERROR) continue;

        int rep_len = GENLMSG_PAYLOAD(&task_msg.n);
        struct nlattr *na = (struct nlattr *)GENLMSG_DATA(&task_msg);
        int len = 0;

        while (len < rep_len) {
            len += NLA_ALIGN(na->nla_len);

            if (na->nla_type == TASKSTATS_TYPE_AGGR_PID ||
                na->nla_type == TASKSTATS_TYPE_AGGR_TGID) {

                int aggr_len = NLA_PAYLOAD(na->nla_len);
                struct nlattr *inner = (struct nlattr *)NLA_DATA(na);
                int len2 = 0;

                while (len2 < aggr_len) {
                    if (inner->nla_type == TASKSTATS_TYPE_STATS) {
                        struct taskstats *t =
                            (struct taskstats *)NLA_DATA(inner);

                        char svc[128];
                        if (identify_service(t->ac_pid, svc, sizeof(svc)) == 0)
                            found_count++;
                        else
                            strcpy(svc, "unknown");

                        get_wall_clock_ms(ts_buf, sizeof(ts_buf));
                        fprintf(out, "%s %d %s %s %llu %llu\n",
                                ts_buf,
                                t->ac_pid,
                                t->ac_comm,
                                svc,
                                (unsigned long long)t->cpu_run_real_total,
                                (unsigned long long)t->cpu_delay_total);

                        exit_count++;
                    }
                    len2 += NLA_ALIGN(inner->nla_len);
                    inner = (struct nlattr *)((char *)inner +
                            NLA_ALIGN(inner->nla_len));
                }
            }
            na = (struct nlattr *)(GENLMSG_DATA(&task_msg) + len);
        }

        if (++flush_counter >= 50) {
            fflush(out);
            flush_counter = 0;
        }
    }

    send_cmd(task_fd, family_id, getpid(), TASKSTATS_CMD_GET,
             TASKSTATS_CMD_ATTR_DEREGISTER_CPUMASK,
             mask_buf, strlen(mask_buf) + 1);

    fprintf(stderr, "ephemeral_monitor: stopped (exits=%llu, identified=%llu, unknown=%llu)\n",
            exit_count, found_count, exit_count - found_count);

    fclose(out);
    close(task_fd);
    return 0;
}

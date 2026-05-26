/*
 * test_cg_exit.c — Test whether /proc/<pid>/cgroup is readable
 * at the moment taskstats delivers the exit event.
 *
 * Receives 200 exit events via taskstats, tries to read cgroup for each.
 * Reports how many succeed (FOUND) vs fail (MISS).
 *
 * Compile: gcc -O2 -o test_cg_exit test_cg_exit.c
 * Run:     timeout 10 ./test_cg_exit
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <linux/netlink.h>
#include <linux/genetlink.h>
#include <linux/taskstats.h>

#define GENLMSG_DATA(glh) ((void *)(NLMSG_DATA(glh) + GENL_HDRLEN))
#define GENLMSG_PAYLOAD(glh) (NLMSG_PAYLOAD(glh, 0) - GENL_HDRLEN)
#define NLA_DATA(na) ((void *)((char *)(na) + NLA_HDRLEN))
#define NLA_PAYLOAD(len) ((len) - NLA_HDRLEN)

struct msgtemplate {
    struct nlmsghdr n;
    struct genlmsghdr g;
    char buf[1024];
};

static int send_cmd(int sd, __u16 type, __u32 pid,
                    __u8 cmd, __u16 ntype, void *data, int dlen) {
    struct msgtemplate msg;
    struct nlattr *na;
    struct sockaddr_nl addr;

    memset(&msg, 0, sizeof(msg));
    memset(&addr, 0, sizeof(addr));
    addr.nl_family = AF_NETLINK;

    msg.n.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
    msg.n.nlmsg_type = type;
    msg.n.nlmsg_flags = NLM_F_REQUEST;
    msg.n.nlmsg_pid = pid;
    msg.g.cmd = cmd;
    msg.g.version = 1;

    na = (struct nlattr *)GENLMSG_DATA(&msg);
    na->nla_type = ntype;
    na->nla_len = dlen + NLA_HDRLEN;
    memcpy(NLA_DATA(na), data, dlen);
    msg.n.nlmsg_len += NLMSG_ALIGN(na->nla_len);

    return sendto(sd, &msg, msg.n.nlmsg_len, 0,
                  (struct sockaddr *)&addr, sizeof(addr));
}

int main(void) {
    int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_GENERIC);
    if (fd < 0) { perror("socket"); return 1; }

    struct sockaddr_nl local;
    memset(&local, 0, sizeof(local));
    local.nl_family = AF_NETLINK;
    int bufsz = 1048576;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bufsz, sizeof(bufsz));
    bind(fd, (struct sockaddr *)&local, sizeof(local));

    /* Get family ID */
    char name[32];
    strcpy(name, TASKSTATS_GENL_NAME);
    send_cmd(fd, GENL_ID_CTRL, getpid(), CTRL_CMD_GETFAMILY,
             CTRL_ATTR_FAMILY_NAME, name, strlen(name) + 1);

    struct { struct nlmsghdr n; struct genlmsghdr g; char buf[256]; } ans;
    recv(fd, &ans, sizeof(ans), 0);
    struct nlattr *na = (struct nlattr *)GENLMSG_DATA(&ans);
    na = (struct nlattr *)((char *)na + NLA_ALIGN(na->nla_len));
    __u16 fid = *(__u16 *)NLA_DATA(na);

    char mask[] = "0-39";
    send_cmd(fd, fid, getpid(), TASKSTATS_CMD_GET,
             TASKSTATS_CMD_ATTR_REGISTER_CPUMASK, mask, sizeof(mask));

    printf("Listening for 200 exit events...\n");

    struct msgtemplate msg;
    int found = 0, notfound = 0, total = 0;

    while (total < 200) {
        int rc = recv(fd, &msg, sizeof(msg), 0);
        if (rc < 0) continue;
        if (msg.n.nlmsg_type == NLMSG_ERROR) continue;

        int rep_len = GENLMSG_PAYLOAD(&msg.n);
        na = (struct nlattr *)GENLMSG_DATA(&msg);
        int len = 0;

        while (len < rep_len) {
            len += NLA_ALIGN(na->nla_len);

            if (na->nla_type == TASKSTATS_TYPE_AGGR_PID ||
                na->nla_type == TASKSTATS_TYPE_AGGR_TGID) {

                int alen = NLA_PAYLOAD(na->nla_len);
                struct nlattr *inner = (struct nlattr *)NLA_DATA(na);
                int l2 = 0;

                while (l2 < alen) {
                    if (inner->nla_type == TASKSTATS_TYPE_STATS) {
                        struct taskstats *t =
                            (struct taskstats *)NLA_DATA(inner);

                        char path[64], line[512], svc[128];
                        svc[0] = '\0';

                        snprintf(path, sizeof(path),
                                 "/proc/%d/cgroup", t->ac_pid);
                        FILE *f = fopen(path, "r");
                        if (f) {
                            while (fgets(line, sizeof(line), f)) {
                                char *p = strstr(line, "/system.slice/");
                                if (p) {
                                    p += 14;
                                    char *nl = strchr(p, '\n');
                                    if (nl) *nl = '\0';
                                    char *sl = strchr(p, '/');
                                    if (sl) *sl = '\0';
                                    if (strstr(p, ".service"))
                                        strncpy(svc, p, 127);
                                    break;
                                }
                            }
                            fclose(f);
                        }

                        if (svc[0]) {
                            found++;
                            if (found <= 15)
                                printf("FOUND pid=%-8d comm=%-16s svc=%s run=%llu\n",
                                       t->ac_pid, t->ac_comm, svc,
                                       (unsigned long long)t->cpu_run_real_total);
                        } else {
                            notfound++;
                            if (notfound <= 5)
                                printf("MISS  pid=%-8d comm=%-16s\n",
                                       t->ac_pid, t->ac_comm);
                        }
                        total++;
                    }
                    l2 += NLA_ALIGN(inner->nla_len);
                    inner = (struct nlattr *)((char *)inner +
                            NLA_ALIGN(inner->nla_len));
                }
            }
            na = (struct nlattr *)(GENLMSG_DATA(&msg) + len);
        }
    }

    printf("\n=== RESULTS ===\n");
    printf("total=%d  found=%d (%.1f%%)  notfound=%d (%.1f%%)\n",
           total, found, found * 100.0 / total,
           notfound, notfound * 100.0 / total);

    send_cmd(fd, fid, getpid(), TASKSTATS_CMD_GET,
             TASKSTATS_CMD_ATTR_DEREGISTER_CPUMASK, mask, sizeof(mask));
    close(fd);
    return 0;
}

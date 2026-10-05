# Prebuilt collector

`cgroup_cpu_snap` — vendored from bpf-collector (`bpf/prebuilt/cgroup_cpu_snap`), version **0.1.0**.

Deploy on AHV under `/root/cgroup_cpu_snap` (AHV mounts `/tmp` as `noexec`).

Source of truth for builds: the bpf-collector repo. Re-copy after rebuilding there:

```bash
cp ../bpf_collector/bpf/prebuilt/cgroup_cpu_snap bin/cgroup_cpu_snap
chmod +x bin/cgroup_cpu_snap
```

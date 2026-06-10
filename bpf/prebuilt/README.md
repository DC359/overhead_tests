# Prebuilt `schedstat_snap`

`schedstat_snap` here is a precompiled, **statically-libbpf-linked** build of the
BPF snapshot+exit collector. The `bpfsnap` collector ships it straight to the AHV
host and runs it — no `clang`/`bpftool`/`libbpf-devel` is needed on the host or CVM.

If this binary is present, `statsCollector/bpfSnapCollector.py` skips the on-host
build entirely (see its `_use_prebuilt` path).

## Why prebuilt

AHV hosts (and the CVM) have no build toolchain. So we compile once on a separate
Linux box and commit the result.

## How it was built

Built on a CentOS Stream 8 VM (`clang 17`, `bpftool`), targeting the el9 AHV host
(kernel `6.18.x el9`). `libbpf` is linked **statically** because el8 ships
`libbpf.so.0` while the el9 host has `libbpf.so.1` — a dynamic link would not load
on the host. `libelf` / `libz` / `glibc` stay dynamic (compatible across el8 -> el9;
an el8-built glibc binary runs on the newer el9 glibc).

Steps (see `../build_static.sh`):

1. Capture the target host's BTF as `bpf/vmlinux.h` (gitignored; regenerate, don't commit):
   ```bash
   bpftool btf dump file /sys/kernel/btf/vmlinux format c > bpf/vmlinux.h
   ```
   Run that on the **target AHV host** (kernel must match what you deploy to), then
   copy `vmlinux.h` next to the sources on the build box.
2. On the build box (with `clang`, `bpftool`, `git`, `make`, `elfutils-libelf-devel`,
   `zlib-devel`):
   ```bash
   cd bpf && bash build_static.sh
   ```
   It builds a static `libbpf.a` from upstream `libbpf` (tag `v0.5.0`, matching the
   build box's `bpftool` skeleton ABI), compiles the CO-RE BPF object + skeleton, and
   links the loader against the static `libbpf.a`.
3. Verify it has **no** dynamic `libbpf` dependency, then drop the result here:
   ```bash
   ldd schedstat_snap   # must NOT list libbpf.so.*
   cp schedstat_snap prebuilt/schedstat_snap
   ```

## Runtime notes

- The AHV host mounts `/tmp`, `/var/tmp`, `/home` as `noexec`; the collector deploys
  and runs the binary from `/root/bpfsnap`.
- The host still needs the kernel/runtime features the collector preflight checks
  (BTF, `bpf_iter` task support, `sched_process_exit`) — those are kernel features,
  not packages, so they can't be installed.

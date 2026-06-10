#!/usr/bin/env bash
# Build a portable, statically-libbpf-linked schedstat_snap on an el8 build box,
# targeting the el9 AHV host. vmlinux.h must already be present in this dir
# (captured from the target host's BTF).
#
#   libbpf : statically linked (built from source, tag matches el8 bpftool ABI)
#   libelf / libz / glibc : dynamic (compatible sonames el8 -> el9)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

LIBBPF_VER="${LIBBPF_VER:-v0.5.0}"
PREFIX="$HERE/.libbpf-install"

if [ ! -f vmlinux.h ]; then
  echo "ERROR: vmlinux.h missing (must be captured from the target el9 host's BTF)" >&2
  exit 1
fi

# 1) Build a static libbpf.a from source (same major as el8 bpftool's skeleton ABI).
LIBA="$(ls "$PREFIX"/lib*/libbpf.a 2>/dev/null | head -1 || true)"
if [ -z "$LIBA" ]; then
  rm -rf libbpf
  git clone --depth 1 -b "$LIBBPF_VER" https://github.com/libbpf/libbpf.git
  make -C libbpf/src BUILD_STATIC_ONLY=1 PREFIX="$PREFIX" install install_uapi_headers
  LIBA="$(ls "$PREFIX"/lib*/libbpf.a 2>/dev/null | head -1)"
fi
INC="$PREFIX/include"
echo "[build] libbpf static: $LIBA"
echo "[build] libbpf headers: $INC"

ARCH=$(uname -m | sed 's/x86_64/x86/;s/aarch64/arm64/')

# 2) Compile BPF object (CO-RE; relocates to the target kernel at load time).
clang -g -O2 -target bpf -D__TARGET_ARCH_${ARCH} -I. -I"$INC" \
      -c schedstat_snap.bpf.c -o schedstat_snap.bpf.o

# 3) Generate the skeleton (distro bpftool; ABI matches libbpf $LIBBPF_VER).
bpftool gen skeleton schedstat_snap.bpf.o > schedstat_snap.skel.h

# 4) Link the loader against the STATIC libbpf; elf/z/glibc stay dynamic.
cc -g -O2 -I. -I"$INC" schedstat_snap.c -o schedstat_snap \
   "$LIBA" -lelf -lz

echo "[build] done:"
file schedstat_snap
echo "[build] dynamic deps (must NOT list libbpf):"
ldd schedstat_snap

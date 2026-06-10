#!/usr/bin/env bash
# Build a portable, statically-libbpf-linked schedstat_snap on an el8 build box,
# targeting the el9 AHV host. vmlinux.h must already be present in this dir
# (captured from the target host's BTF).
#
# IMPORTANT: the host runs a modern kernel (6.x) whose BTF uses BTF_KIND_ENUM64,
# which only libbpf >= 1.0 can parse. So we build a MODERN libbpf (and a matching
# bpftool for skeleton generation) from source — the distro/el8 libbpf 0.5 is too
# old and fails at load with "failed to find valid kernel BTF" (-3).
#
#   libbpf : statically linked, built from source at $BPFTOOL_VER's submodule
#   bpftool: built from source (same libbpf) — used for `gen skeleton`
#   libelf / libz / glibc : dynamic (compatible sonames el8 -> el9)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

BPFTOOL_VER="${BPFTOOL_VER:-v7.5.0}"
SRC="$HERE/.bpftool-src"
PREFIX="$HERE/.libbpf-install"

if [ ! -f vmlinux.h ]; then
  echo "ERROR: vmlinux.h missing (capture it from the target el9 host's BTF)" >&2
  exit 1
fi

# 1) Clone bpftool + its libbpf submodule (modern libbpf understands 6.x BTF).
if [ ! -d "$SRC/.git" ]; then
  rm -rf "$SRC"
  git clone --depth 1 -b "$BPFTOOL_VER" --recurse-submodules \
      https://github.com/libbpf/bpftool.git "$SRC"
fi

# 2) Build bpftool from source (used to generate the skeleton).
make -C "$SRC/src" -j"$(nproc)"
BPFTOOL="$SRC/src/bpftool"
echo "[build] bpftool: $("$BPFTOOL" version 2>&1 | head -1)"

# 3) Build a static libbpf.a from the SAME submodule (ABI matches the bpftool above).
rm -rf "$PREFIX"
make -C "$SRC/libbpf/src" BUILD_STATIC_ONLY=1 PREFIX="$PREFIX" \
     install install_uapi_headers
LIBA="$(ls "$PREFIX"/lib*/libbpf.a 2>/dev/null | head -1)"
INC="$PREFIX/include"
echo "[build] libbpf static: $LIBA"

ARCH=$(uname -m | sed 's/x86_64/x86/;s/aarch64/arm64/')

# 4) Compile BPF object (CO-RE; relocates to the target kernel at load time).
clang -g -O2 -target bpf -D__TARGET_ARCH_${ARCH} -I. -I"$INC" \
      -c schedstat_snap.bpf.c -o schedstat_snap.bpf.o

# 5) Generate the skeleton with the MODERN bpftool (ABI matches the static libbpf).
"$BPFTOOL" gen skeleton schedstat_snap.bpf.o > schedstat_snap.skel.h

# 6) Link the loader against the STATIC modern libbpf; elf/z/glibc stay dynamic.
cc -g -O2 -I. -I"$INC" schedstat_snap.c -o schedstat_snap \
   "$LIBA" -lelf -lz

echo "[build] done:"
file schedstat_snap
echo "[build] dynamic deps (must NOT list libbpf):"
ldd schedstat_snap

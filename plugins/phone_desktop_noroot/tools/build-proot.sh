#!/bin/bash
# Rebuilds device/proot (static arm64 Termux proot + talloc) on an x86_64 Ubuntu 24.04 host.
set -e
# Needs: sudo apt install g++-aarch64-linux-gnu qemu-user-static git curl
# Output: ~/proot-arm64/proot/src/proot
W=~/proot-arm64; mkdir -p $W; cd $W
TALLOC=talloc-2.4.2
[ -f $TALLOC.tar.gz ] || curl -sSLf -o $TALLOC.tar.gz https://www.samba.org/ftp/talloc/$TALLOC.tar.gz
sha256sum $TALLOC.tar.gz
rm -rf $TALLOC && tar xzf $TALLOC.tar.gz && cd $TALLOC
CC=aarch64-linux-gnu-gcc ./configure --prefix=$W/sysroot --disable-python --disable-rpath \
  --cross-compile --cross-execute="qemu-aarch64-static -L /usr/aarch64-linux-gnu" >../talloc-configure.log 2>&1 || { tail -30 ../talloc-configure.log; exit 1; }
make -j8 >../talloc-make.log 2>&1 || { tail -30 ../talloc-make.log; exit 1; }
make install >/dev/null

aarch64-linux-gnu-gcc -O2 -fPIC -D_GNU_SOURCE -D__STDC_WANT_LIB_EXT1__=1 -DHAVE_CONFIG_H -Ibin/default -Ilib/replace -I. -c talloc.c -o $W/talloc.o
aarch64-linux-gnu-gcc -O2 -fPIC -D_GNU_SOURCE -D__STDC_WANT_LIB_EXT1__=1 -DHAVE_CONFIG_H -Ibin/default -Ilib/replace -I. -c lib/replace/replace.c -o $W/replace.o
cd $W; rm -f libtalloc.a; aarch64-linux-gnu-ar rcs libtalloc.a talloc.o replace.o
[ -d proot ] || git clone -q https://github.com/termux/proot.git
cd proot && git checkout -q 0e42875 && git log -1 --format='%h %ad %s' && cd src
make clean >/dev/null 2>&1 || true
make -j8 V=1 HAS_LOADER_32BIT= CC=aarch64-linux-gnu-gcc LD=aarch64-linux-gnu-gcc STRIP=aarch64-linux-gnu-strip \
  OBJCOPY=aarch64-linux-gnu-objcopy OBJDUMP=aarch64-linux-gnu-objdump \
  CFLAGS="-O2 -I$W/sysroot/include" LDFLAGS="-static -L$W -ltalloc" proot >$W/proot-make.log 2>&1 || { grep -i -m15 "error" $W/proot-make.log; tail -15 $W/proot-make.log; exit 1; }
aarch64-linux-gnu-strip proot

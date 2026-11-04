# Builds, inside a throwaway debian container, a local apt repository (file:/repo, a flat repository
# with a hand-written Release) in which three installed packages have a newer version in a security
# pocket (Origin Ubuntu, Version 24.04, Suite noble-security), and one of them (kernel-demo-azure)
# now depends on a package that is not installed (newdep). `apt-get -s upgrade` keeps that kernel back;
# `apt-get -s dist-upgrade` upgrades it and installs newdep. Used by guest.sh; needs no network.
set -eu
mk() { # name version depends
  d=/tmp/pkg-$1-$2; rm -rf $d; mkdir -p $d/DEBIAN
  { echo "Package: $1"; echo "Version: $2"; echo "Architecture: all"; echo "Maintainer: demo <demo@example.invalid>"; echo "Description: demo $1"
    [ -n "${3:-}" ] && echo "Depends: $3"; } > $d/DEBIAN/control
  dpkg-deb -b $d /tmp/$1_$2_all.deb >/dev/null
}
mk libdemo 1.0; mk kernel-demo-azure 1.0; mk openssl-demo 1.0
mk libdemo 1.1; mk kernel-demo-azure 1.1 "newdep (>= 1.0)"; mk newdep 1.0; mk openssl-demo 1.1
dpkg -i /tmp/libdemo_1.0_all.deb /tmp/kernel-demo-azure_1.0_all.deb /tmp/openssl-demo_1.0_all.deb >/dev/null
mkdir -p /repo; cp /tmp/libdemo_1.1_all.deb /tmp/kernel-demo-azure_1.1_all.deb /tmp/newdep_1.0_all.deb /tmp/openssl-demo_1.1_all.deb /repo/
cd /repo
: > Packages
for f in *.deb; do
  dpkg-deb -f $f >> Packages
  { echo "Filename: ./$f"; echo "Size: $(stat -c %s $f)"; echo "MD5sum: $(md5sum $f | cut -d' ' -f1)"; echo "SHA256: $(sha256sum $f | cut -d' ' -f1)"; echo; } >> Packages
done
{ echo "Origin: Ubuntu"; echo "Label: Ubuntu"; echo "Suite: noble-security"; echo "Version: 24.04"; echo "Codename: noble"; echo "Date: Tue, 04 Nov 2026 00:00:00 UTC"; echo "Architectures: all"; echo "Components: main"
  echo "SHA256:"; for f in Packages; do echo " $(sha256sum $f | cut -d' ' -f1) $(stat -c %s $f) $f"; done; } > Release
echo "deb [trusted=yes] file:/repo ./" > /etc/apt/sources.list
rm -rf /etc/apt/sources.list.d/*
apt-get update >/dev/null 2>&1 || apt-get update

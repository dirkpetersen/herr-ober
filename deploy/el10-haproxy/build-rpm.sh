#!/bin/sh
# Build HAProxy 3.4 RPMs for EL10 (RHEL, AlmaLinux, Rocky 10) from the Fedora source RPM.
# EL10 ships only HAProxy 3.0. Run as root on an EL10 build host of the target architecture.
# Output: RPMS/<arch>/haproxy-3.4*.rpm under the printed directory.
set -eu
SRPM_URL=${SRPM_URL:-https://kojipkgs.fedoraproject.org/packages/haproxy/3.4.8/1.fc46/src/haproxy-3.4.8-1.fc46.src.rpm}

dnf -y install rpm-build dnf-plugins-core
if command -v subscription-manager >/dev/null && grep -q "Red Hat" /etc/redhat-release; then
    subscription-manager repos --enable "codeready-builder-for-rhel-10-$(arch)-rpms"  # RHEL
else
    dnf config-manager --set-enabled crb  # AlmaLinux, Rocky
fi
top=$(mktemp -d /var/tmp/haproxy-rpm.XXXXXX)
cd "$top"
curl -fsSLO "$SRPM_URL"
rpm -i --define "_topdir $top" ./*.src.rpm
# Fedora merged /usr/sbin into /usr/bin, EL10 did not: the admin tools install to %{_sbindir}
sed -i 's#%{_bindir}/\(halog\|iprange\|ip6range\)$#%{_sbindir}/\1#' SPECS/haproxy.spec
dnf -y builddep SPECS/haproxy.spec
rpmbuild --define "_topdir $top" -bb SPECS/haproxy.spec
ls "$top"/RPMS/*/haproxy-*.rpm

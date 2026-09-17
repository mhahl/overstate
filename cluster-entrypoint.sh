#!/bin/bash
# Pod-aware entrypoint for the salt-master cluster.
#
# Salt elects the cluster founder by lexical sort over
# ``{interface} + cluster_peers``; the lowest wins and everyone else
# runs discover/join. The shared ConfigMap cannot hold a per-pod
# `interface`, and the base image regenerates /etc/salt/master at every
# boot (wiping anything baked in) — so with the default 0.0.0.0 every
# pod sorts first, every pod declares itself founder, and each
# bootstraps a SOLO cluster. Hence this wrapper: once the generated
# file lands, pin `interface` to this pod's StatefulSet DNS name before
# supervisord starts the daemon a few seconds later.
#
# The name (not the IP) is what makes the election deterministic:
# salt-master-0 sorts first on every pod, so pod 0 founds and pod 1
# joins, on every boot. Outside Kubernetes (dev compose) POD_NAME is
# unset and the base behavior is untouched.
set -u
MASTER_CONF=/etc/salt/master

if [ -n "${POD_NAME:-}" ]; then
  (
    for _ in $(seq 1 600); do
      if [ -s "$MASTER_CONF" ] && ! grep -q '^interface:' "$MASTER_CONF"; then
        printf '\n# Pod address for cluster founder election (POD_NAME).\ninterface: %s.salt-master\n' "$POD_NAME" >>"$MASTER_CONF"
      fi
      if grep -q '^interface:' "$MASTER_CONF" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
  ) &
fi

exec /sbin/entrypoint.sh "$@"

#!/bin/sh
# Renders the proxy allowlist and client ACL, then runs Squid in the foreground.
#
# EGRESS_ALLOWLIST   comma-separated exact hosts or `*.domain` (subdomains only, never the
#                    apex), with the same semantics as SOURCE_EGRESS_ALLOWLIST. Empty means
#                    deny everything.
# EGRESS_CLIENT_CIDR the backend network CIDR allowed to use the proxy.
set -eu
# No pathname expansion: an entry such as "*" must be validated, never globbed to files.
set -f
# Split entries on newlines only (commas are translated below), never on spaces.
IFS='
'

state_dir=/run/squid
allowlist="$state_dir/allowlist.regex"
clients="$state_dir/clients.txt"
: > "$allowlist"

entries=$(printf '%s' "${EGRESS_ALLOWLIST:-}" | tr 'A-Z' 'a-z' | tr ',' '\n')
count=0
for raw in $entries; do
  entry=$(printf '%s' "$raw" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/\.$//')
  [ -n "$entry" ] || continue
  case "$entry" in
    \*.*) wildcard=1; host=${entry#\*.} ;;
    *) wildcard=0; host=$entry ;;
  esac
  # At least two labels of [a-z0-9-], not all-numeric TLD: no "*", "*.com", or IPs.
  if ! printf '%s' "$host" | grep -Eq '^([a-z0-9-]+\.)+[a-z0-9-]*[a-z-][a-z0-9-]*$'; then
    echo "egress-proxy: invalid allowlist entry: $entry" >&2
    exit 64
  fi
  escaped=$(printf '%s' "$host" | sed 's/\./\\./g')
  if [ "$wildcard" -eq 1 ]; then
    printf '^([a-z0-9-]+\\.)+%s\\.?$\n' "$escaped" >> "$allowlist"
  else
    printf '^%s\\.?$\n' "$escaped" >> "$allowlist"
  fi
  count=$((count + 1))
done
if [ "$count" -eq 0 ]; then
  # Squid needs at least one value; this one can never match a real hostname.
  printf '^no-host-is-allowed\\.invalid$\n' > "$allowlist"
  echo "egress-proxy: EGRESS_ALLOWLIST is empty; all egress is denied" >&2
fi

cidr=${EGRESS_CLIENT_CIDR:-}
if ! printf '%s' "$cidr" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}/([89]|[12][0-9]|3[0-2])$'; then
  echo "egress-proxy: EGRESS_CLIENT_CIDR must be an IPv4 CIDR (/8 to /32)" >&2
  exit 64
fi
printf '%s\n' "$cidr" > "$clients"

echo "egress-proxy: $count allowlist entr(y/ies), clients $cidr" >&2
squid -k parse -f /etc/squid/squid.conf >/dev/null 2>&1 || {
  squid -k parse -f /etc/squid/squid.conf
  exit 65
}
exec squid -N -f /etc/squid/squid.conf

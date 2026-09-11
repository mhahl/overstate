# Topology coverage: proxy minions, syndic, salt-ssh

Overstate talks to one salt-api only. These notes describe how non-standard
topologies behave and where the limits are.

## Proxy minions

Proxy minions already appear in key/minion lists because they use the same
key and job paths. Their grains differ per proxy type: keys may be missing
and `ipv4` may be a scalar. The minion list, detail, and CSV export coerce
grains defensively (missing keys render blank, scalar `ipv4` becomes a
one-item list). Rows carrying a `proxytype`/`proxyid` grain get a `proxy`
badge. No proxy-specific actions exist; run ordinary jobs against them.

## Syndic

Set `SYNDIC_MASTERS` (comma-separated) so the Keys page shows a read-only
banner: the roster shown is the local master's only, and key actions never
propagate upstream. There is no multi-master support: run one Overstate per
master you manage. Key accept/reject/delete here affects the local master.

## salt-ssh

The Run-job form offers a salt-ssh transport. Limits (documented, enforced
where stated):

- Sync only: async requests over ssh are forced to sync with a notice.
- Timeout is 180s per call; large rosters fan out over SSH, so expect
  minutes, not seconds.
- Returns carry no JID: the job is recorded complete under a synthetic
  `ssh-<timestamp>` id with the `run-ssh:<fun>` audit action. No per-minion
  return rows are stored and the detail page shows no returns.
- Targeting follows the master's roster, not live minions (`ignore_invalid`
  is set so unknown roster entries fail the call instead of hanging it).
- Roster editing is out of scope: manage `/etc/salt/roster` on the master.

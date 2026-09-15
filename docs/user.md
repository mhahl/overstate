# Overstate user guide

Overstate puts a web UI in front of your Salt master. You accept keys,
run jobs, apply states, and watch results here. Salt still does the work.
Every button maps to a real Salt call, and the UI shows you the job ID
so you can trace what happened.

## Log in

Open the app URL your admin gave you. Type your username and password.
After 10 login submissions from your address within a minute, the
login page answers 429 until the window passes. A successful login
resets your counter.

If your site uses single sign-on, the login page shows a second button,
**Log in with SSO**. It takes you to your identity provider and back.
SSO accounts start as viewers. Ask an admin for a higher role.

Log out with the logout button. It uses POST, so no prefetch or
tab-restore logs you out by accident.

## Roles

Three roles exist. Each higher role includes everything below it.

- **Viewer.** You see everything: minions, jobs, states, pillar,
  schedules, events, reactor. You change nothing.
- **Operator.** You run jobs, accept keys, manage schedules and
  reactors, toggle
  beacons, capture pillar snapshots, and edit watched states. You
  cannot touch users, settings, or roles.
- **Admin.** You do all of that plus users and settings.

Buttons for actions above your role do not appear, and the server
rejects the request anyway if you craft one by hand.

## Dashboard

The dashboard answers one question: is the fleet healthy right now.

- **Keys.** Accepted versus pending counts. A growing pending pile
  means new minions wait for you on the Keys page.
- **Minions up / down / stale.** Live numbers from the master.
  Down means the master cannot reach the minion. Stale means Overstate
  has not seen it recently.
- **Jobs in flight.** Jobs the database still marks incomplete.
- **Last failures.** The five most recent failed returns, newest first.
- **salt-api health.** The URL, token age, last probe, and whether the
  service account may call wheel and runner endpoints. If this panel
  shows unreachable, every other number on the page is suspect. Tell
  your admin.

## Minions

The minion list is a cache of grains snapshots, not live data. Salt
remains the source of truth. When a row looks wrong, hit refresh.

### List, search, filter

Search matches minion IDs. Filters narrow by presence status. Pages
follow the page-size setting. Columns come from grains: OS fingerprint,
OS release, FQDN, IP addresses, CPU architecture, CPU count, memory,
virtualization role, and Salt version. Open the detail page for the
full grain set.

Secondary tabs split the list three ways: all minions, groups, and
stale ones. Stale means the snapshot is older than your site expects.
Check whether the minion is actually down before you delete anything.

### Refresh

The refresh button asks every reachable minion for fresh grains and
rewrites the snapshot cache. Small fleets finish in seconds. Big fleets
take longer because the call fans out across the whole fleet with a
30-second timeout.

### Export

**Export CSV** downloads the current list. Open it in any spreadsheet.
Use it for reports and asset reconciliation.

### Groups

The Groups card below the list stores named member lists. Open New
group, name it, and pick members from the full roster in the select
box (Use checked fills it from the ticked rows). Then choose the
group target type on the job form to fire at exactly those members.
Edit reopens the same dialog; Delete removes the group. Members
that vanished from inventory resolve out at fire time with a note.

### Minion detail

Click a minion ID for its detail page. Tabs organize the facts:

- **Overview.** Grains, presence, key status.
- **States.** Last highstate result per state.
- **Jobs.** Recent jobs that touched this minion.
- **Schedule.** That minion's scheduled jobs.
- **Pillar.** Pillar data, read-only.
- **Beacons.** Beacon list with enable / disable toggles.
  Definitions live in pillar and are never edited here.
- **Mine.** This minion's stored mine values: enter a mine
  function to see what it last reported.

### Onboard a new minion

The onboard wizard builds the shell commands for a fresh machine.
Reach it from the Onboard minion button on the Minions list or the
Onboard a minion link on the pending Keys tab. Pick the distribution
(openSUSE or Fedora), type the minion ID and the master hostname,
and the page renders a join script. Download it, run it on the new
machine as root, then come back: the minion appears in
the pending list. Acceptance happens on the Keys page, not here.
The wizard shows pending keys and whether the master is reachable so
you know where you stand.

## Keys

Keys prove minion identity. A minion generates its own keypair on first
start and sends the public half to the master. Nothing runs on that
minion until you accept the key. No auto-accept exists. This is
deliberate.

Four tabs split keys by state: Pending, Accepted, Rejected, Denied.
Each row shows the fingerprint. Compare it against the fingerprint on
the minion itself before you accept:

```sh
salt-key --finger-all
```

or on the minion:

```sh
salt-call --local key.finger
```

Accept moves the key to accepted. Reject and delete remove it. Deleted
minions that still run reappear as pending, which is normal.

## Jobs

### New job

The job form has five parts.

1. **Target.** A matcher plus its type: glob (`web*`), list
   (comma-separated IDs), grain (`osfinger:openSUSE*`), compound
   (boolean combinations), nodegroup (a named group defined on the
   master), or group (a member list you saved on the Minions page;
   unknown IDs resolve out at fire time).
2. **Function.** Any execution module function the service account may
   call, for example `test.ping` or `pkg.install`.
3. **Arguments.** Space-separated, passed through as given.
4. **Mode.** Async returns a job ID at once and tracks results as they
   land. Sync waits for the return before the page loads. salt-ssh
   always runs sync; the form tells you when it switches.
5. **Via.** Local (normal publish) or salt-ssh (agentless over SSH).

Preset buttons fill the form for common work: ping, highstate,
dry-run highstate (`test=True`), state apply, package install and
remove, and process signal. Selecting minions in the list and choosing
a bulk action suggests a glob covering exactly those IDs.

Destructive functions (`pkg.install`, `pkg.remove`,
`service.restart`, `ps.kill_pid`) ask for confirmation: retype the
target to prove you aimed where you meant. The **save as** field
stores the finished form as a named saved job for reuse.

State applies get a review step with an SLS preview: the page
lists the states about to be enforced, rendered on the first
matched minion (base environment) with foldable raw data. The
preview is advisory — if rendering fails, a note says so and
firing stays available.

For wide targets, switch batch mode from off to a wave size (count
or percent) and set a stop-after-failures limit. The run pins the
matching minions from inventory, executes wave by wave with one job
per wave, and stops early when failures reach your limit. The job
page shows every wave with its own returns plus a banner with the
running total; Stop batch cancels between waves. Batch mode takes
list, glob, and saved-group targets.

### Running, history, saved

Three tabs organize jobs. Running shows jobs the database marks
incomplete. History shows finished jobs, newest first, sortable by
start time, function, and user. Saved shows your named forms; one
click reloads a form into the editor, and a delete button removes it.

### Job detail

The detail page shows the job record plus every return the master
stored. Highstate output renders folded with the summary first; expand
a minion to see its state-by-state result. Raw JSON stays available
for copy-paste. The page polls for new returns while the job runs, so
leave it open and watch minions check in. The **sync** button pulls
the latest state from the master on demand. Each return links back to
its minion.

A running job shows a **Kill job** button. It sends SIGKILL for that
job to the job's own target; per-minion kill reports land in a card
on the same page. Minions that stay silent are unknown, not dead.

### Orchestrate

The Orchestrate link beside Run job opens the orchestration runner:
name the orchestration state, pick the environment, toggle dry run,
and optionally add a pillar override as JSON. Long runs execute in
the background. Returns land in History and on the job page like any
other job.

## States and conformity

The States page tracks whether minions match their assigned
configuration. Each minion carries one of four verdicts:

- **ok.** The last highstate applied cleanly.
- **drifted.** The last run reported changes or failures.
- **unknown.** No highstate result on record yet.
- **unreachable.** The job targeted this minion but no return arrived
  before the job aged out. Check presence and key state; an
  untargeted minion keeps its prior verdict instead of going blank.

Verdicts update themselves as job returns land; each one links to the
job that produced it, with the check time and a short history trail.
**Recompute** replays stored state-job returns into fresh verdicts
without running anything new — a backfill, not the normal path.

Watched states narrow the verdict to specific SLS files instead of the
whole highstate. Add an SLS name to the watch list, remove it when you
stop caring. When the list is non-empty, a minion is ok only if every
watched file came back clean; returns that cannot be narrowed fall
back to the whole-job verdict and carry a **partial** badge so you
know. Filter by status, sort by minion ID or status, and open a minion
to see its last stored run state by state.

The minion detail States tab shows the last stored run first, so a
down minion never hangs the page. **Refresh from minion** asks once
for a live description and shows it next to the stored run without
rewriting history; when the minion cannot answer, the stored data
stays with a note saying so.

## Schedules

Schedules are Salt's built-in cron: recurring jobs that live on the
minion. Pick a minion to list its entries. Each entry shows the
function, interval, and whether it is enabled.

Add needs a name, a function, an interval value plus unit (seconds,
minutes, hours, days), and the enabled flag. The name must be unique
on that minion. Enable and disable flip the flag without deleting the
entry. Delete removes it. Every change runs through salt-api, so a
minion that is down reports an error instead of pretending.

## Beacons

Beacons watch things on the minion (processes, load, files) and fire
events. Their definitions live in pillar, so Overstate never edits
them here — the same read-only rule as the Pillar page.

The Beacons tab on a minion lists each configured beacon with its
configuration. Entries defined in pillar carry a pillar badge;
minion-local ones are marked minion. Enable and disable flip a
minion-local beacon's runtime state without touching its
definition. Pillar-badged beacons refuse both actions. Salt
answers "configured in pillar" and the page shows that error
instead of a false success, so change the definition in pillar
instead. Every toggle runs through salt-api, so a minion that is
down reports an error instead of pretending, and each successful
toggle lands in the audit trail.

## Mine

The Mine page reads cached facts minions reported: pick a target,
a target type, and a mine function (for example
`network.ip_addrs`) to see per-minion values. Mine data is a
cache — minions push it on their own schedule — so the page says
so and links the mine-update preset to refresh it. An empty
result means nothing is stored for that function, not an error.

## Pillar

Pillar is per-minion secret configuration: passwords, API tokens,
role flags. Overstate reads pillar and snapshots it. It never writes
pillar.

The pillar index lists minions and how many snapshots each one keeps.
Open a minion to compare the live pillar against stored snapshots.
**Capture** stores the current live pillar as a new snapshot. The diff
view compares any two snapshots, or two minions, side by side. Use
diffs to answer "what changed on this host last Tuesday" and to check
that a whole role shares identical secrets.

## Files

The file browser shows the Salt file roots the app was pointed at:
top files and SLS content, rendered as text with line numbers. Search
narrows the listing, directories group the rows, and long listings
page. Files too large or not readable as text show an explainer card
instead of a blank error. The browser never edits content: no edit
button exists and none is planned.

The top of the page shows the git checkout behind the listing:
branch, revision, clean or uncommitted state, how far it sits from its
upstream, and the last commits. If a file you pushed has not appeared,
the checkout is behind — press **Check for updates** (operators only)
to refresh the behind count without touching files, then **Sync now**
to pull the latest. A changed pull also refreshes the master
fileserver so applies see the new states at once; if that refresh
fails you get a warning, never a failed sync. Sync is fast-forward
only: a diverged checkout or an uncommitted tree refuses with the git
reason and changes nothing, and every attempt lands in the audit
trail. If the page says the path is not a checkout, sync is
unavailable there; ask your admin.

## Events

The event viewer tails the Salt event bus through salt-api. Pick one
or more families: job events, auth events, minion lifecycle, key
events, runner events. The server filters by tag prefix and keeps the
last 50 events per stream for 60 seconds, so open the stream before
you run the job you want to watch. Payloads stay on the server; the
browser receives tags plus small summaries. Raw bus content never
reaches the page.

## Reactor

The Reactor page lists the master's event → SLS mapping exactly as
`reactor.list` reports it, with each SLS viewable read-only. Reactor
rows link to the matching Events family so you can watch them fire.
Operators can add and delete mappings (delete asks once on its own
page); every change is audit-logged. The mapping is master-config
state, not git content — but **Export for git** renders the live
mapping as a `reactor:` YAML block you can commit by hand. The app
never writes your repo.

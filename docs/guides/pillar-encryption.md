# Encrypted pillar values (NaCl) for NetBird and IPA secrets

NetBird setup keys and IPA passwords live in pillar so states can
consume them, but pillar files are committed to the canonical states
repo in plaintext by default. This guide encrypts those values with
Salt's **nacl** renderer (sealedbox): git and the shared `srv-data`
volume hold only `NACL[...]` ciphertext, and each master decrypts at
render time.

Verified against this deployment: Salt 3008.2 masters, PyNaCl 1.6.2
baked into the master image, daemon runs as `salt` (1000:1000).

## What encryption does and does not cover

Covered: the values are ciphertext in git history, in the Files
browser, and on the shared volume. Anyone reading the repo sees
`NACL[...]` strings, not passwords.

Not covered: the master decrypts before serving, so `pillar.items`
returns plaintext. Overstate's Pillar page shows that plaintext to
anyone holding `pillar.read`, and **Capture** snapshots store it in
Postgres in plaintext (backups included). Treat encrypted pillar as
protection for git at rest, not as access control inside the UI —
scope `pillar.read`/`secrets-reader` accordingly (see
`docs/guides/admin.md` and `docs/reference/rbac.md`). Minions always
receive their rendered pillar in plaintext over the AES channel; that
is unchanged Salt behavior, not something this guide introduces.

## 1. Generate the keypair on a master

One keypair for the whole master trio. The key pair is 32 raw bytes per
key, base64-encoded by Salt. Generate it on `salt-master-0` so it lands
on that pod's keys PVC:

```sh
NS=overstate
KEYDIR=/home/salt/data/keys/nacl
kubectl exec -n $NS salt-master-0 -- bash -c "
  install -d -m 0700 -o salt -g salt $KEYDIR &&
  runuser -u salt -- salt-call --local nacl.keygen sk_file=$KEYDIR/nacl"
```

This writes `$KEYDIR/nacl` (secret) and `$KEYDIR/nacl.pub` (public).
There is no passphrase: the masters must decrypt unattended on every
render, so the protection boundary is Kubernetes RBAC plus the offline
backup below — not the key itself.

Read the public key (safe to share; paste it into Overstate):

```sh
kubectl exec -n $NS salt-master-0 -- runuser -u salt -- cat $KEYDIR/nacl.pub
```

Back up the secret key offline (vault, sealed envelope — wherever the
`salt-master-keys` Secret material lives). It is the only copy that can
ever decrypt the values below.

## 2. Copy the secret key to the other masters

Each master decrypts locally against its own keys PVC, so the same
secret key goes on `salt-master-0`, `-1`, and `-2`. Pod-to-pod copying
pipes the key through `kubectl exec`:

```sh
NS=overstate
KEYDIR=/home/salt/data/keys/nacl
SK=$(kubectl exec -n $NS salt-master-0 -- runuser -u salt -- cat $KEYDIR/nacl)
for POD in salt-master-1 salt-master-2; do
  printf '%s\n' "$SK" | kubectl exec -i -n $NS $POD -- bash -c "
    install -d -m 0700 -o salt -g salt $KEYDIR &&
    cat > $KEYDIR/nacl &&
    chown salt:salt $KEYDIR/nacl &&
    chmod 0600 $KEYDIR/nacl"
done
```

Confirm each pod holds a 32-byte base64 key:

```sh
for POD in salt-master-0 salt-master-1 salt-master-2; do
  kubectl exec -n $NS $POD -- runuser -u salt -- wc -c $KEYDIR/nacl
done
```

A rescheduled pod keeps its keys PVC, so the key survives. If a keys
PVC is ever deleted, re-run this step from the offline backup.

## 3. Enable the nacl renderer in master config

These settings now live in the repo manifest
(`deploy/kubernetes/salt-master-config.yaml`), so they survive every
`kubectl apply -k`. Verify they are present, then restart the masters
one pod at a time (Master Settings tab → restart, or
`kubectl rollout restart statefulset/salt-master`; the PDB keeps
quorum):

```yaml
renderer: jinja | yaml | nacl
nacl.config:
  box_type: sealedbox
  sk_file: /home/salt/data/keys/nacl/nacl
  pk_file: /home/salt/data/keys/nacl/nacl.pub
```

The renderer decrypts any string matching `NACL[...]` inside the
rendered pillar data. A missing PyNaCl in the master image surfaces as
`'nacl' __virtual__ returned False`, and a wrong `sk_file` surfaces as
a render error on the minion's next `pillar.items` — never as silent
plaintext.

## 4. Write the encrypted pillar files

In the canonical states repo, next to `salt/`:

`pillar/top.sls`:

```yaml
base:
  '*':
    - secrets.prod
```

`pillar/secrets/prod.sls` — values are the `NACL[...]` envelopes Salt
produces. Encrypt with the **public** key only:

```sh
salt-call --local nacl.enc 'YOUR-NETBIRD-SETUP-KEY' pk_file=/home/salt/data/keys/nacl/nacl.pub
# -> base64 sealed box; wrap it as NACL[...] in the pillar file
```

```yaml
baseline:
  netbird:
    join:
      setup_key: "NACL[<base64 sealed box>]"
```

Larger files (certificates) use `nacl.enc_file` / `nacl.dec_file`.

Commit and push through the Files page as usual (or use the Encrypted
pillar editor, which does the encryption in the app). States consume
the decrypted values with no special handling:

```yaml
netbird-up:
  cmd.run:
    - name: netbird up --setup-key {{ pillar['baseline']['netbird']['join']['setup_key'] }}
    - unless: netbird status >/dev/null 2>&1
```

Quote the Jinja expansion when the value may contain shell-special
characters (passwords almost always do).

## 5. Verify end to end

1. On a minion: `salt '<minion>' pillar.items` (Jobs page, `pillar.items`
   function) shows the secret in **plaintext** — that is the proof
   decryption works.
2. In git (`git log -p` on the pillar files) and in the Files browser
   the same values are `NACL[...]` — that is the proof encryption holds.
3. In Overstate, open the minion's Pillar page: plaintext again, as
   expected, with the capture warning. Capture a snapshot only if you
   accept it landing in Postgres.
4. The Encrypted pillar page's **Verify** action does all of this
   automatically on the chosen minion.

## 6. Rotation

**Rotating a value** (new NetBird key, new IPA password): re-encrypt
with the existing public key, replace the envelope, commit, then
`saltutil.refresh_pillar` (Jobs page preset) so minions pick it up
without waiting for the mine interval.

**Rotating the keypair** (compromise or hygiene): generate a new pair,
copy the new secret key to all three masters, paste the new public key
in **Settings → Infrastructure → Encrypted pillar** (checkbox-gated),
re-enter every value from plaintext (sealedbox cannot re-encrypt what
the app cannot decrypt), push, restart the masters, then delete the old
secret key from each keys PVC and from the offline backup.

## Managing from Overstate (web UI)

Overstate can drive steps 4–6 without a workstation. It never holds the
secret key: only the master **public** key is stored (as an app
setting), values are encrypted in memory when you save, and the
`NACL[...]` ciphertext alone lands in the pillar tree. Sealedbox is
one-way, so the app cannot decrypt anything.

**One-time setup**

1. **Pillar → Encrypted pillar** shows the onboarding checklist: the
   app reads the live master ConfigMap and reports whether the `nacl`
   renderer and `nacl.config` are set (step 3 above). The admins-only
   **Set up NaCl on the masters** button runs steps 1–2 for you over
   the Kubernetes exec API (the app's role already grants
   `pods/exec`): keygen on the first master, the same secret key
   installed on the others with a digest check, and the public key
   stored automatically. An existing key is reused, never regenerated,
   so the button doubles as a repair for a master that lost its key.
   The copy-paste accordion stays for clusters where the app has no
   exec access.
2. **Settings → Infrastructure → Encrypted pillar** stores the base64
   public key and shows its key id. Replacing the key requires the
   confirmation checkbox — existing values stop decrypting until they
   are re-entered.
3. **Pillar → Encrypted pillar → Verify** writes a throwaway encrypted
   key into a wired collection, runs `saltutil.refresh_pillar` on one
   test minion, reads `pillar.items` back and checks the plaintext
   matches, then removes the key. A green result proves the whole path
   against the real master.

**Daily use**

- Create a collection (one `pillar/secrets/<name>.sls`), wire it into
  `pillar/top.sls` with one click (idempotent), then add secrets by
  dotted key (`baseline.netbird.join.setup_key`). Values are typed as
  plaintext and encrypted with the stored public key.
- **Replacing a value is blind**: the app cannot show the old
  plaintext, so updates ask for a checkbox confirmation and always
  overwrite. `NACL[...]` strings produced elsewhere cannot be imported;
  enter the plaintext instead.
- Each save is a single-file git commit (same flow as the Files page).
  Push when you are ready; the master reads the working tree, so
  `saltutil.refresh_pillar` (Jobs preset) is what makes minions pick
  up a new value.
- Deleting a key removes it from the file and commits.

**What this does not change**

- `pillar.items` returns decrypted values, so `pillar.read` holders
  (including `secrets-reader`) and **Capture** snapshots still see
  plaintext in the UI and Postgres — see "What encryption does and does
  not cover" at the top of this guide.
- Keypair rotation stays a manual master-side operation (step 6). The
  web UI's job is storing the new public key and re-entering values.
- A rescheduled master missing its keys PVC still decrypts nothing
  until the secret key is restored; the verify flow surfaces that
  immediately.

## Failure modes

- `'nacl' __virtual__ returned False: PyNaCl import error` in master
  logs: the master image lacks PyNaCl — rebuild it with the
  `pynacl` pin from `Containerfile.salt-master`.
- `pillar.items` fails to render after step 3: `sk_file` wrong or the
  secret key missing on the master that served the request — re-run the
  copy loop and confirm all three pods hold the key.
- `salt-call --local nacl.keygen` writes `nacl.pub` beside `sk_file`;
  if you point `sk_file` at a path the `salt` user cannot write, the
  keygen fails or lands unreadable — keep the directory `0700
  salt:salt`.
- A pod rebuilt onto a fresh keys PVC decrypts nothing until the key
  is restored; the symptom is identical to a wrong `sk_file`.

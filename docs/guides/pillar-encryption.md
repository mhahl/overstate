# Encrypted pillar values (GPG) for NetBird and IPA secrets

NetBird setup keys and IPA passwords live in pillar so states can
consume them, but pillar files are committed to the canonical states
repo in plaintext by default. This guide encrypts those values with
Salt's GPG renderer: git and the shared `srv-data` volume hold only
ciphertext, and each master decrypts at render time.

Verified against this deployment: Salt 3008.2 masters, `gpg` 2.4.8 and
`python-gnupg` already in the master image (no rebuild needed), daemon
runs as `salt` (1000:1000).

## What encryption does and does not cover

Covered: the values are ciphertext in git history, in the Files
browser, and on the shared volume. Anyone reading the repo sees
`-----BEGIN PGP MESSAGE-----` blocks, not passwords.

Not covered: the master decrypts before serving, so `pillar.items`
returns plaintext. Overstate's Pillar page shows that plaintext to
anyone holding `pillar.read`, and **Capture** snapshots store it in
Postgres in plaintext (backups included). Treat encrypted pillar as
protection for git at rest, not as access control inside the UI —
scope `pillar.read`/`secrets-reader` accordingly (see
`docs/guides/admin.md` and `docs/reference/rbac.md`). Minions always
receive their rendered pillar in plaintext over the AES channel; that
is unchanged Salt behavior, not something this guide introduces.

## 1. Generate the pillar keypair (offline workstation)

One keypair for the whole master trio. No passphrase: the masters must
decrypt unattended on every render, so the protection boundary is
Kubernetes RBAC plus the offline backup below — not the key itself.

```sh
gpg --quick-generate-key "overstate-pillar" rsa4096 cert never
KEYID=$(gpg --list-keys --with-colons overstate-pillar | awk -F: '/^pub:/{getline; print $10; exit}')
gpg --export --armor -o pillar-gpg.pub "$KEYID"
gpg --export-secret-keys --armor -o pillar-gpg.key "$KEYID"
```

Back up `pillar-gpg.key` offline (vault, sealed envelope — wherever
the `salt-master-keys` Secret material lives). It is the only copy
that can ever decrypt the values below. The public key
(`pillar-gpg.pub`) is safe to keep on the workstation; only it is
needed to encrypt new values.

## 2. Import the private key on all three masters

Each master decrypts locally against its own keys PVC, so the private
key goes on `salt-master-0`, `-1`, and `-2`. The GPG homedir must be
`0700` and owned by the `salt` user — the daemon runs as uid 1000,
not root.

```sh
NS=overstate
KEYDIR=/home/salt/data/keys/gpgkeys
for POD in salt-master-0 salt-master-1 salt-master-2; do
  kubectl cp pillar-gpg.key $NS/$POD:/tmp/pillar-gpg.key
  kubectl exec -n $NS $POD -- bash -c "
    install -d -m 0700 -o salt -g salt $KEYDIR &&
    runuser -u salt -- gpg --homedir $KEYDIR --batch --import /tmp/pillar-gpg.key &&
    shred -u /tmp/pillar-gpg.key &&
    runuser -u salt -- gpg --homedir $KEYDIR --list-secret-keys"
done
```

Each pod should list the `overstate-pillar` secret key. Nothing
secret may remain in `/tmp` afterwards (`shred -u` handles that).

A rescheduled pod keeps its keys PVC, so the keyring survives. If a
keys PVC is ever deleted, re-run this step from the offline backup.

## 3. Enable the GPG renderer in master config

Settings → **Master Settings** tab (admins only), edit `master.conf`,
append below the managed line:

```yaml
renderer: jinja | yaml | gpg
gpg_keydir: /home/salt/data/keys/gpgkeys
```

Save (snapshots automatically), then restart the masters one pod at a
time from the same page and wait for the rollout plus salt-api health
before touching the next pod. The PDB keeps quorum (2 of 3) throughout.

Verify the renderer loads by rendering a test value (step 5 covers a
real secret; a wrong `gpg_keydir` surfaces as a render error on the
minion's next `pillar.items`, never as silent plaintext).

## 4. Write the encrypted pillar files

In the canonical states repo, next to `salt/`:

`pillar/top.sls`:

```yaml
base:
  '*':
    - secrets.netbird
    - secrets.ipa
```

`pillar/secrets/netbird.sls` — encrypt on the workstation with the
**public** key only (`printf %s` avoids a trailing newline in the
secret):

```sh
printf '%s' 'YOUR-NETBIRD-SETUP-KEY' | gpg --encrypt --armor --trust-model always -r "$KEYID"
```

```yaml
netbird:
  setup_key: |
    -----BEGIN PGP MESSAGE-----

    <paste the full armored block, indented>
    -----END PGP MESSAGE-----
```

`pillar/secrets/ipa.sls`, same procedure for the enrollment password:

```sh
printf '%s' 'YOUR-IPA-ADMIN-PASSWORD' | gpg --encrypt --armor --trust-model always -r "$KEYID"
```

```yaml
ipa:
  admin_password: |
    -----BEGIN PGP MESSAGE-----

    <paste the full armored block, indented>
    -----END PGP MESSAGE-----
```

Commit and push through the Files page as usual. States consume the
decrypted values with no special handling:

```yaml
netbird-up:
  cmd.run:
    - name: netbird up --setup-key {{ pillar['netbird']['setup_key'] }}
    - unless: netbird status >/dev/null 2>&1
```

```yaml
ipa-enroll:
  cmd.run:
    - name: ipa-client-install --unattended --password '{{ pillar['ipa']['admin_password'] }}' ...
    - unless: test -f /etc/ipa/default.conf
```

Quote the Jinja expansion when the value may contain shell-special
characters (passwords almost always do).

## 5. Verify end to end

1. On a minion: `salt '<minion>' pillar.items` (Jobs page, `pillar.items`
   function) shows `setup_key` / `admin_password` in **plaintext** —
   that is the proof decryption works.
2. In git (`git log -p` on the pillar files) and in the Files browser
   the same values are PGP blocks — that is the proof encryption holds.
3. In Overstate, open the minion's Pillar page: plaintext again, as
   expected. Capture a snapshot only if you accept it landing in
   Postgres.

## 6. Rotation

**Rotating a value** (new NetBird key, new IPA password): re-encrypt
with the existing public key, replace the block, push, then
`saltutil.refresh_pillar` (Jobs page preset) so minions pick it up
without waiting for the mine interval.

**Rotating the keypair** (compromise or hygiene): generate a new pair,
import the new private key on all three masters, re-encrypt every
block with the new public key, push, restart the masters, then delete
the old secret key from each homedir (`runuser -u salt -- gpg
--homedir $KEYDIR --batch --yes --delete-secret-keys OLDKEYID`) and
from the offline backup.

## Failure modes

- `pillar.items` fails to render on a minion after step 3: `gpg_keydir`
  wrong or the private key missing on the master that served the
  request — re-run the import loop and confirm all three pods list the
  secret key.
- A pod rebuilt onto a fresh keys PVC decrypts nothing until the key
  is re-imported; the symptom is identical to the above.
- `gpg: WARNING: unsafe permissions` in master logs: the homedir is
  not `0700 salt:salt` — fix ownership, GPG refuses to decrypt
  otherwise.

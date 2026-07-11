# Sparkle Key Custody and Feed Operations

## Purpose

This runbook owns the production Sparkle EdDSA key boundary and the stable
GitHub Pages endpoint for direct-DMG updates. It complements the architecture in
[sparkle-updates.md](sparkle-updates.md).

The private key must never be committed, copied into issue or pull-request text,
printed in logs, or uploaded as a workflow artifact.

## Provisioned Infrastructure

- Sparkle key account: `cbusillo-BD_to_AVP`.
- Sparkle working-key service: `https://sparkle-project.org` in the maintainer's
  login keychain.
- Recovery store: an Apple Passwords entry synchronized by iCloud Keychain.
- GitHub environment: `sparkle-release`.
- Target environment protection: protected `main` only, with no separate
  required-review rule. Release intent is approved once at `macos-signing`.
- Reserved environment secret: `SPARKLE_EDDSA_PRIVATE_KEY`.
- Production public key: `sparkle-public-ed-key.txt`.
- Feed URL: `https://cbusillo.github.io/BD_to_AVP/appcast.xml`.
- Emergency empty-feed source: `sparkle-feed/appcast.xml`, deployed only by the
  manual `.github/workflows/manage-sparkle-pages.yml` workflow.
- Durable feed history: the cumulative `appcast.xml` asset attached to every
  published GitHub Release.

The release workflow's appcast job uses the `sparkle-release` environment and
receives the private key only in its signing step. Manual feed management first
passes the reviewer-protected, secret-free `sparkle-feed-ops` environment, then
calls the same validated Pages workflow without referencing the private key.

## Current State

GitHub Pages uses the GitHub Actions deployment source, and the live feed is
bound by `appcast-state.json` to a verified durable release snapshot. The
`sparkle-release` environment is limited to `main` and does not add a second
approval after the run has passed the `macos-signing` release-intent gate.

The production working key is stored in the maintainer's login keychain, its
recovery copy is imported into Apple Passwords, and
`SPARKLE_EDDSA_PRIVATE_KEY` is stored only in the protected `sparkle-release`
environment. The matching public key is committed at
`sparkle-public-ed-key.txt` for #163 and future recovery verification.

## One-Time Provisioning

Perform this sequence on the maintainer's Mac with Sparkle's pinned
`generate_keys` binary. Verify the archive digest documented in
[sparkle-updates.md](sparkle-updates.md) before extraction.

1. Disable shell tracing and set `umask 077`.
2. Create and mount a temporary RAM disk. All exported private-key and Passwords
   import files must stay on that volume.
3. Run `generate_keys --account cbusillo-BD_to_AVP` once. The working private
   key remains in the login keychain.
4. Read the public key with
   `generate_keys --account cbusillo-BD_to_AVP -p` and save only that public
   value in the repository for #163.
5. Export the private key with
   `generate_keys --account cbusillo-BD_to_AVP -x <ram-disk-file>`.
6. Build a one-row Apple Passwords CSV import file on the RAM disk with the
   header `Title,URL,Username,Password,Notes`. Use:
   - title: `BD_to_AVP Sparkle EdDSA Recovery`;
   - URL: `https://sparkle-project.org`;
   - username: `cbusillo-BD_to_AVP`;
   - password: the exported private-key value; and
   - notes: the public key and repository name.
7. Import that file with the Passwords app and confirm the entry is visible in
   the maintainer's iCloud-synchronized password collection.
8. Pipe the RAM-disk key file through standard input with the explicit repository
   target; never use a command-line `--body` value:

   ```sh
   gh secret set SPARKLE_EDDSA_PRIVATE_KEY \
     --env sparkle-release \
     --repo cbusillo/BD_to_AVP
   ```

9. Verify GitHub reports the secret name under `sparkle-release`, not at
   repository scope.
10. Re-read the working public key and confirm it matches the recorded public
    key.
11. Eject the RAM disk, which destroys all temporary plaintext material, and
    clear any clipboard content used during the import.

The Apple Passwords import is intentionally interactive. An unsigned command-line
process cannot create an iCloud-synchronizable Keychain item; macOS rejects that
operation with `errSecMissingEntitlement`.

## Recovery Test

Before enabling appcast signing in #165:

1. Create a fresh RAM disk.
2. Copy the password value from the iCloud-synchronized recovery entry into a
   file on that RAM disk.
3. Import the file with
   `generate_keys --account bd-to-avp-recovery-test -f <ram-disk-file>`.
4. Read the throwaway account's public key with
   `generate_keys --account bd-to-avp-recovery-test -p` and confirm it matches
   the committed production public key.
5. Delete only the throwaway item, then eject the RAM disk:

   ```sh
   security delete-generic-password \
     -s https://sparkle-project.org \
     -a bd-to-avp-recovery-test
   ```

This proves the recovery copy is usable without replacing the production
working key.

## Environment Use

The appcast-signing job must:

- declare `environment: sparkle-release`;
- run only from protected `main` and package only the validated `github.sha`;
- check out and execute only the protected workflow revision in the key-bearing
  job;
- receive the private key only as
  `${{ secrets.SPARKLE_EDDSA_PRIVATE_KEY }}`;
- pipe the secret to Sparkle with `--ed-key-file -`;
- avoid shell tracing and command-line secret arguments; and
- never upload signing workspaces or key files as artifacts.

## Feed Disable and Key Loss

- To disable updates without changing installed apps, dispatch
  `Manage Sparkle Pages` from protected `main` with the `disable` operation and
  no tag. It preempts an in-flight feed deployment, deploys the valid empty
  appcast plus a disabled `appcast-state.json`, and leaves every GitHub Release
  and cumulative snapshot unchanged. Release and normal deploy workflows fail
  closed until restore clears that state.
- To restore updates, dispatch `Manage Sparkle Pages` from protected `main` with
  `restore` and a published release tag. The workflow validates and redeploys
  that release's durable `appcast.xml` asset.
- If the GitHub secret is lost but the recovery entry remains, restore the
  secret from the recovery entry through a RAM-disk file and standard input.
- If the working login-keychain item is lost, restore it from the recovery entry
  with `generate_keys --account cbusillo-BD_to_AVP -f`, then verify the public
  key before signing anything.
- If all approved private-key copies are lost, automatic updates cannot resume
  with the installed public key. Publish a manually installed app build
  containing a new public key before a new automatic-update chain begins.

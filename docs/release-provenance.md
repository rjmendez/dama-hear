# Firmware release provenance: SBOM, signed attestation, verification and revocation

Dated 2026-09-15. **Nothing here has been executed and no release has been cut.** This document
is the design and the operating procedure for the provenance half of a `hear_node` firmware
release: what a published release says about itself, what signs that statement, what an operator
can check offline, how each part fails, and how a bad release is withdrawn.

It closes the gap [release-v0.1.6-readiness.md](release-v0.1.6-readiness.md) §6 recorded honestly
and did not fix: *"manifest + `SHA256SUMS` only; no SBOM, no signed attestation."*

Read [ota-release-credentials.md](ota-release-credentials.md) for the credential contract this is
built on. Nothing here changes it: a published asset still carries no Wi-Fi credentials, no node
identity, no push token and no admin token, and this work adds **no secret of its own**.

## 1. The problem a manifest alone cannot solve

`release-manifest.json` and `SHA256SUMS` are both *self-referential*. They describe the assets
published beside them, and anyone who can publish assets can publish a matching pair. Together
they prove **internal consistency** — these five files are the five files this document meant —
and they prove nothing at all about **origin**: not who built them, not from which commit, not
with which toolchain, not whether the workflow that claims to have produced them ever ran.

Two questions therefore have no answer today:

| Question | Answered by |
|---|---|
| "What is inside this image?" | an SBOM |
| "Who built it, from what, and did this repository really produce it?" | a signed attestation |

## 2. What is published, and how the four documents interlock

A release publishes, per variant, `.bin`, `-bootloader.bin`, `-partitions.bin`, `-merged.bin`
and `.elf` (see readiness §2), plus:

| Asset | What it asserts | Who checks it |
|---|---|---|
| `build-info.json` | tag, commit, FQBN, toolchain versions, `image_class: unprovisioned` | `release_manifest.py` |
| `release-manifest.json` | per-artifact SHA-256, source closure, capture profile, **SBOM hash**, **attestation policy** | `flash.py`, `enroll.py`, `release_manifest.py verify` |
| `release-manifest.schema.json` | the manifest's own schema | `release_manifest.py verify` |
| `release-sbom.cdx.json` | CycloneDX 1.6 SBOM: binaries, source closure, vendored libraries, toolchain pins | `release_sbom.py verify`, both installers |
| `release-provenance.intoto.jsonl` | Sigstore bundle: SLSA v1 provenance over **every file above** | `gh attestation verify`, both installers |
| `SHA256SUMS` | checksums of everything, including the bundle | operators, legacy path |

The chain is deliberately acyclic, in this order:

```
binaries ──▶ release-sbom.cdx.json ──▶ release-manifest.json ──▶ attestation bundle ──▶ SHA256SUMS
             (hashes the binaries)      (hashes the SBOM)         (signs all of the above)
```

⚠️**The manifest does not hash the bundle, on purpose.** The bundle's in-toto subjects include the
manifest, so a manifest that hashed the bundle could never be generated. `SHA256SUMS` covers the
bundle; the bundle covers the manifest; the manifest covers every image and the SBOM. Each link is
checkable on its own, and no link points backwards.

## 3. Artifact ↔ variant linkage

The manifest already binds every asset to the variant it belongs to — `board_class`,
`psram_mode`, `release_stem`, `fqbn`, `build_flags`, the board header's own hash and the derived
capture profile — and `board_profiles.release_variant_refusal()` refuses to install one variant's
image on a node of the other (`gold` is quad, its class default is octal).

The SBOM repeats that linkage *per file* as CycloneDX properties, so a single component answers
"which board is this for, built with which flags, from which FQBN":

```
dama-hear:board-class, dama-hear:psram-mode, dama-hear:release-stem,
dama-hear:fqbn, dama-hear:build-flags, dama-hear:artifact-kind,
dama-hear:image-class, dama-hear:compiled-in-credentials
```

The attestation's subjects are the files themselves, by name and SHA-256, so the signature binds
the variant linkage transitively: the manifest and the SBOM that carry it are signed subjects.

## 4. Source commit and build environment

Recorded by the manifest (unchanged, already merged): repository, commit, `commit_short`,
`git describe`, dirty flag and the exact dirty paths, with a refusal to generate a manifest at all
from a dirty tree unless `--allow-dirty` writes an explicitly unverifiable one. Plus sketch, FQBN,
`--libraries`, `arduino-cli` and esp32 core versions, and a SHA-256 of every source input,
generated header (with its generator's hash) and schema guard.

Added by the attestation: the SLSA v1 `buildDefinition` — the workflow file, its repository, the
ref and the runner — asserted by GitHub's build system rather than by the build's own output.
`release_manifest.py verify --attestation` checks the workflow path and repository in that
predicate and refuses a bundle produced by a different workflow or a mirror repository.

## 5. SBOM scope and format

**Format:** CycloneDX 1.6 JSON (`release-sbom.cdx.json`). Chosen over SPDX because the consumers
that matter here — `gh attestation`, Trivy, Syft, Dependency-Track and GitHub's dependency
tooling — all read CycloneDX JSON, and the repository's OCI images already emit CycloneDX SBOMs
through `docker/build-push-action` (`sbom: true` in `images.yml`). One format across images and
firmware.

**Scope, stated inside the document** (`metadata.properties["dama-hear:sbom-scope"]`):

*Covered:* the published binaries; the source closure the manifest records; every vendored library
under `firmware/lib`, content-addressed by a digest over its own file tree; the pinned toolchain
versions.

*Not covered, and said so:*

* the **contents** of the esp32 board package — its ESP-IDF fork, its gcc toolchain and the
  second-stage bootloader it ships — which are pinned by version, not enumerated. The release
  does not build them and cannot attest to them; claiming otherwise would make the SBOM a more
  dangerous document than no SBOM at all;
* runtime data a node is provisioned with (NVS record, Wi-Fi, tokens) — never in a public image;
* host tooling an operator happens to have installed.

**Derived, not written.** `release_sbom.py` computes the SBOM from the same tree and the same
`dist/` directory that `release_manifest.py` computes the manifest from, using the same code path.
A document that disagrees with the release is a failing check, not a stale file.

**Deterministic.** No timestamp and no random serial number: the serial is a UUIDv5 of
(repository, tag, commit). Two runs on one commit produce byte-identical SBOMs, which is what lets
the manifest hash it and what makes a rebuild comparable.

## 6. Attestation and signing identity

`actions/attest-build-provenance@v4`, in `release.yml`, over `subject-path: dist/*`.

⚠️**Keyless. There is no signing key, in `secrets` or anywhere else.** The job asks for an OIDC
token (`id-token: write`); Sigstore exchanges it for a short-lived certificate whose identity is
`https://github.com/rjmendez/dama-hear/.github/workflows/release.yml@refs/tags/<tag>`; the
certificate expires in minutes and the signature is logged in the public transparency log.
`attestations: write` records the result against the repository.

This is the only signing scheme this repository can adopt without inventing a key-custody
procedure it would then have to run — and a repository whose entire release contract is *"a public
asset must never contain a credential"* has no business acquiring a long-lived signing key.

**A fork cannot obtain this identity.** `release.yml` runs on `push: tags: v*` only; pull requests
do not run it, and a fork's OIDC token carries the fork's repository, which
`--signer-workflow rjmendez/dama-hear/...` rejects.

**The bundle is published as a release asset.** Verification therefore needs no GitHub credential
and no API access to the repository's attestation store.

## 7. Verification: downloaded release, and offline

### An operator, before flashing anything

```sh
gh attestation verify hear_node-esp32s3-i2s-gps-v0.1.6.bin \
  --repo rjmendez/dama-hear \
  --signer-workflow rjmendez/dama-hear/.github/workflows/release.yml \
  --bundle release-provenance.intoto.jsonl

python3 firmware/hear_node/release_manifest.py verify --dist dist --tag v0.1.6 --attestation
python3 firmware/hear_node/release_sbom.py verify --dist dist --tag v0.1.6
```

`release_manifest.py verify --attestation` checks, **offline**: the schema, every artifact hash,
the release-metadata artifacts, the SBOM's hash and its agreement with the manifest, and that the
bundle's subjects cover every published file including the manifest itself, from the right
repository and the right workflow. It does **not** check the signature, and it says so in its own
output; `--verify-signature` shells out to `gh attestation verify` for that.

| What you have | What can be checked | What cannot |
|---|---|---|
| Release directory + bundle, no network | hashes, manifest, SBOM, subject coverage, workflow identity in the predicate | the signature (needs a Sigstore trusted root) |
| The above + `gh` + network | everything, cryptographically | — |
| Assets only, no manifest (v0.1.5 and earlier) | `SHA256SUMS` | origin, contents, everything else |

### The installers

`flash.py --release` and `enroll.py --release` do this automatically, in this order, and refuse
before a byte reaches a node:

1. **Revocation** — the tag is not in `firmware/hear_node/release_revocations.json`. Checked
   before the first HTTP request.
2. **Manifest** — hashes match the downloaded bytes; the release does not claim compiled-in
   credentials; the source was not dirty.
3. **SBOM** — declared in the manifest ⇒ fetched, hash-checked against the manifest, and
   cross-checked component by component. Declared and missing is a refusal.
4. **Attestation** — declared ⇒ fetched and required to cover this asset *and* the manifest.
   Declared and missing is a refusal, overridable only with `--allow-unattested`, which then says
   so in the output.
5. **Signature** — only with `--verify-signature` (needs `gh`; needs no credential). Without it
   the printed line reads `signature NOT checked`, never `verified`.

⚠️**A check that could not run is never reported as a check that passed.** `gh` missing is an
error, not a silent skip.

### Releases that predate all of this

A v0.1.5 manifest declares neither `sbom` nor `attestation`. Both installers accept it and both
say exactly what they verified. Refusing it would strand the fleet on firmware it cannot update
away from — the rankine failure one level up.

## 8. Failure modes

| Failure | What happens | Response |
|---|---|---|
| `actions/attest-build-provenance` fails (Sigstore/Fulcio outage) | the release job fails **before** `gh release create`; nothing is published | re-run the job; the tag is untouched |
| Bundle uploaded but covers the wrong digests | the in-job `verify --attestation` fails before publication | fix and re-cut; a tag is cheap |
| SBOM disagrees with the tree | `release_sbom.py verify` fails in the job | regenerate; investigate a dirty or drifted build |
| Manifest declares an SBOM the release does not have | installers refuse the release | re-cut; do not hand-upload a missing asset |
| Operator has no `gh` | structural checks still run; signature is reported as **not checked** | verify on a machine with `gh` before a fleet rollout |
| Sigstore trusted root unreachable | `gh attestation verify` fails closed | do not flash; retry, or verify elsewhere |
| A release asset is edited in place after publication | the manifest hash, the SBOM and the attestation subject all fail | revoke (§9) |
| The repository's OIDC identity is impersonated by a fork | `--signer-workflow` and the predicate's repository check both refuse | report; nothing to rotate — there is no key |

## 9. Rollback and release revocation

⚠️**A published release cannot be recalled.** Mirrors, caches and an operator's `.otabuild/` copy
outlive any deletion, and deleting the release also deletes the manifest that would have explained
why. Deleting a tag is *housekeeping*, not revocation.

So revocation is a fact recorded in the **repository the installers run from**:

`firmware/hear_node/release_revocations.json`

```json
{"tag": "v0.1.6", "date": "2026-09-16",
 "reason": "built from a dirty tree; the manifest is unverifiable",
 "superseded_by": "v0.1.7"}
```

An entry makes `flash.py --release v0.1.6` and `enroll.py --release v0.1.6` refuse before any
download, naming the reason and the successor. A revocation is therefore a **pull request**, with
a reviewer, a date and a reason — not a button.

Procedure when a cut release turns out to be bad:

1. Open a PR adding the entry. Merge it before anything else.
2. State the revocation in the successor tag's release notes and in the GitHub release body of the
   revoked tag (editing notes does not change any asset, so it cannot invalidate the manifest,
   the SBOM or the signature).
3. Cut the successor tag. Never re-cut the same tag: the attestation binds the tag, and two
   different builds claiming one tag is precisely the ambiguity this whole document exists to
   remove.
4. Nodes already flashed are handled by the existing per-image failback and the rollout runbook
   (readiness §8, §9), not by this file. **Never revert a provisioned node to a tokenless asset.**

⚠️**An operator on a stale checkout gets the stale answer.** `git pull` is step 0 of every
rollout, and is why the revocation list lives next to the installer rather than on a server the
installer would have to trust and reach.

## 10. Credential safety

* No signing key, no `secrets.*` reference in `release.yml` — a test asserts the absence.
* `id-token: write` and `attestations: write` are scoped to the `release` job, which runs only on
  a tag push; no pull-request workflow can reach them.
* The SBOM lists file names, hashes and versions. It is generated from a tree that
  `release.yml` has already proven contains no `secrets.h`, and it enumerates no NVS content, no
  token and no network name.
* `gh attestation verify --bundle` reads a local file; an operator never needs to authenticate,
  so no token is created for verification.
* Nothing in this path prints, stores or transports a fleet credential, and the existing refusals
  — no `secrets.h` in the build tree, `image_class: unprovisioned`, `compiled_in_credentials:
  false` — remain the gate on what may be published at all.

## 11. What CI proves

`tests/test_release_provenance_attestation.py`, on every PR:

* the release job asks for OIDC and attestation permissions and reads **no** repository secret;
* it signs every published file, in the right order — SBOM, manifest, attestation, `SHA256SUMS` —
  and verifies its own attestation before `gh release create`;
* the SBOM is byte-identical across runs, is CycloneDX 1.6, covers every binary, the source
  closure, the vendored libraries and the toolchain pins, and states what it excludes;
* a tampered binary, an SBOM from another commit and a swapped SBOM are each refused;
* an attestation that misses a file, covers a different digest, names another workflow or another
  repository, carries the wrong predicate type or is corrupt is refused — and a missing `gh` is
  an error, not a pass;
* the installers fetch and check both documents, refuse a declared-but-missing one, refuse a
  revoked tag before the first request, and never claim a signature they did not check;
* a release with neither block still installs.

## 12. Deliberately not done here

* **No release is cut, no tag is pushed, no asset is published.** This is tooling and procedure.
* **No credential is generated.** The admin-token decision (readiness §5, §11.1) still blocks the
  cut and is untouched by this work.
* **No reproducible-build claim.** `arduino-cli` output is not bit-reproducible across runners
  today; the manifest and the SBOM pin the *inputs*, and the attestation says which builder
  produced the outputs. A rebuild-and-compare gate would be a separate piece of work with its own
  evidence.
* **No SLSA level is claimed.** The release now carries SLSA v1 provenance generated by GitHub's
  hosted builder; calling that "SLSA L3" is an audit conclusion, not a field in a JSON file.
* **`puc_node` is not covered.** It is built by `firmware.yml` but not published as a release
  asset; when it is, it takes the same path.

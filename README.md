# Jefahnierocks organization profile

`profile/README.md` is the organization's public GitHub profile. This repository
also owns its Project Manifest Specification 1 declaration and offline source
gate. Its workflow checks this repository only. Shared community defaults,
workflow templates and organization policy require their own owner decision.

## Current status

The existing profile is unchanged. The Specification 1 interface passed its
native source checks and mise launcher in temporary task state. GitHub Actions
records successful first push and pull-request checks on 2026-10-17 for
[PR #2](https://github.com/jefahnierocks/.github/pull/2) head
`184bf471675e4fc26767082d077b06883f7e2cfc`:
[push verification](https://github.com/jefahnierocks/.github/actions/runs/39066587057)
and [PR verification](https://github.com/jefahnierocks/.github/actions/runs/39066624876).
That dated evidence applies to that head; later revisions need their own native
validation and hosted checks. It establishes neither main landing nor ordinary
persistent-user readiness. No runtime service or producer admission is claimed.
Full child-baseline adoption, including a license decision for this repository's
own material and a standing secret-scanning gate,
remains separate owner work. The vendored MIT release retains its own license.

## Prepare tools

The project pins Python **3.13.16** in `mise.toml` and `mise.lock`. The manifest
reader requires native Git **2.56.0**, pinned in the reader and hosted preparation
script. The stdlib gate has no Python package dependencies.

Owner-only preparation: review and trust this repository's `mise.toml`, then run
`mise install --locked python@3.13.16` if that exact Python is not already present.
Use an already prepared native Git 2.56.0, or request host-owned preparation.
Do not change global runtime pins. Set `PROFILE_MANIFEST_GIT` to its absolute
executable path; scripts and version-manager shims are refused.

```sh
export PROFILE_MANIFEST_GIT=/absolute/path/to/native/git
mise run check
```

For a linked worktree, also set `PROFILE_MANIFEST_COMMON_DIR` to the explicit
physical Git common directory. It selects storage only, never an authority or
identity record. No automatic installation or environment activation is enabled.
Existing tool stores remain tool-owned; ordinary checks use only owned temporary
fixtures, without network requests or persistent reports.

## Commands and layout

- `mise run check` validates the worktree declaration and runs the synthetic tests.
- `mise run test` is the same complete source gate.
- `python -I -S -B scripts/validate.py --rev FULL_OID` validates the specified
  immutable revision's declaration plus the currently checked-out source tests.
  CI checks out that event revision and supplies its full object ID.
- `scripts/check-project-manifest.py` owns the immutable Release 1 binding. The
  bootstrap authenticates all release leaves; only four library modules are
  imported from captured authenticated bytes. Nested released diagnostics remain
  unchanged; the child reports its own declaration verdict separately.
- `tests/` contains synthetic consumer regressions. `vendor/project-manifest/`
  holds the exact public Release 1 payload and separately pinned bootstrap.
- `scripts/prepare-linux-tools.py` is an explicit hosted-only preparation step.
  It downloads checksum-pinned Python and Git into owned runner temporary storage
  and builds Git there. It does not replace runner or global tools. This network
  setup precedes the ordinary offline gate.

No setup, build, server, formatting or secret-delivery command applies to this
profile repository. The declared platforms are the intended qualification lanes,
not runtime or host-admission evidence. Source validity, GitHub profile rendering,
producer acceptance and the separate child-baseline adoption are distinct.

[Release 1](https://github.com/jefahnierocks/project-manifest/releases/tag/release-1)
is the immutable public origin. Re-vendoring requires explicit review of a whole
release and independent bootstrap binding; never update only the vendored reader.

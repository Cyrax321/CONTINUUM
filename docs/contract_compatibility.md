# Recovery-contract compatibility

A `RecoveryContract` is a promise that travels: it is sealed by one build and
verified by another, possibly a different framework's verifier reading only the
JSON. That only works if the bytes the hash covers are *published* rather than
"whatever the model defines today".

This document is that publication. It defines the canonical serialization, the
field set each version covers, what a verifier does with a version it does not
know, and how to add a field or a version. The checked-in corpus
(`src/continuum/recovery/corpus/`) and the conformance suite
(`continuum.recovery.conformance`) make every rule here executable, so a claim
in this document can fail a test.

## Where the rules live

| What | Where |
| --- | --- |
| Version table and digest rules | `src/continuum/recovery/contract.py` (`CONTRACT_VERSIONS`) |
| Canonical digest input | `canonical_digest_input(contract, version)` |
| Digest itself | `contract_digest(contract, version)` |
| Independent checker | `src/continuum/recovery/conformance.py` |
| Corpus fixtures | `src/continuum/recovery/corpus/*.json` |
| Corpus-driven tests | `tests/test_contract_conformance.py` |

## Canonical serialization

The digest input is canonical JSON over the version's covered fields:

```json
{"checkpoint_version":3,"invalidated":["external_dependency:dataset (CONFLICTED)"],"recovery_status":"requires_repair","run_id":"run_1","verified":["goal"]}
```

The rules, all of which an independent verifier must reproduce:

* Keys are sorted.
* No insignificant whitespace: `(",", ":")` separators.
* ASCII only (`ensure_ascii`), so a field value containing non-ASCII text is
  escaped and never depends on the byte order of the source file.
* Only the version's *covered* fields appear. A field the version does not
  cover is omitted entirely, never emitted as `null`.
* `liveness.last_append_age` is stripped when `liveness` is covered: it is
  seconds since the last append at assessment time, so two assessments of an
  unchanged run would otherwise seal different hashes. The verdict fields
  (`breached`, `threshold_seconds`, `phase`, `breaches`) stay covered.
* `created_at`, `integrity_hash` and `contract_version` are never covered. The
  first two are bookkeeping; the third selects which payload to hash, so it
  cannot also be inside it.

The digest is `sha256` over those bytes as UTF-8. That is deliberately the
plainest possible construction: a verifier in another language computes it from
the digest input alone and needs nothing from Python.

## Versions

Each version pins a field set. A verifier recognizes a version or it does not;
there is no partial understanding.

| Version | Covers | Exists because |
| --- | --- | --- |
| 0 | all fields except `evidence`, `reason` | the pre-Phase-1 contract, which had neither field |
| 1 | version 0 plus `evidence`, `reason` | Phase 1 made the contract self-explaining (#289) |

This build seals version 1 (`CURRENT_CONTRACT_VERSION`).

### Compatibility policy

* **New verifier, old contract.** Try the contract's declared version, then
  each older one in descending order. A version-0 payload that predates
  `evidence`/`reason` will not match the version-1 digest -- those fields
  default to empty, and the default is covered -- but it does match the
  version-0 digest, which omits them. Only older versions are tried; a newer
  version's digest covers fields the stored hash cannot contain, so it can
  never match a contract sealed beneath it.
* **Old verifier, new contract.** If the contract carries a version the
  verifier does not know, verification fails closed with a diagnostic naming
  the version and the ones the verifier does know. This is deliberate: a
  verifier cannot know what a future version's hash covers, and guessing is
  how a tampered or semantically different contract gets accepted.
* **Unknown hash-covered field.** The model forbids unknown fields, so a
  payload carrying one does not load at all. Rejection happens before any
  digest is computed.

## The corpus

Each fixture is one checked-in JSON case stating the payload, the version it
claims, the canonical digest input (or `null` when the payload cannot be
canonicalized), and the expected outcome. Categories the policy depends on:

* `current` -- sealed by this build, verifies.
* `legacy` -- sealed before a covered field existed, verifies via an older
  version's rules.
* `forward-extension` -- carries a version this build does not know, fails
  closed.
* `malformed` -- cannot load at all, rejected before any digest.
* `tampering` -- loads but its stored hash disagrees with its own payload.

Run the checker over the whole corpus:

```python
from continuum.recovery.conformance import run_conformance_suite, corpus_summary

results, fixtures = run_conformance_suite()
print(corpus_summary(results))
```

The checker recomputes each digest from the published rules above. It never
calls `verify_contract`: the production verifier and the published rules must
be two things that can disagree, or conformance is a program checking itself.
`tests/test_contract_conformance.py` also runs the production verifier over the
same corpus and asserts agreement, because a second implementation can be wrong
in the same direction as the first.

### Running the corpus from another verifier

The checker takes a directory, so a verifier built in another language or
framework points it at a copy of the corpus and implements one function: given
a payload, return whether it verifies. The digest rules in this document are
the entire contract; nothing else about CONTINUUM is required. A Rust or Go
port that reproduces every fixture's outcome has implemented the published
compatibility surface, and a divergence is a bug filed against this document,
not an integration problem.

## Adding a field or a version

* **Optional, non-covered field** (display or advisory only, like
  `post_checkpoint_observations` and `liveness`): no version bump. An old
  verifier ignores it and the digest is unchanged. This is why those fields can
  be added freely.
* **Covered field** (a new term that the hash must protect): bump the version.
  Add the name to the new version's covered set, leave the old version covering
  what it covered, and add a corpus fixture in each affected category. An old
  verifier reading the new contract now fails closed by design, which is the
  honest outcome -- upgrade the verifier, or stay on the old version.
* **Serialization change** (a covered field's canonical form changes shape):
  bump the version for the same reason.

Every bump lands in `CONTRACT_VERSIONS` with a note, in the version table in
this document, and in the corpus. If a rule changes and no fixture changes,
the rule was not load-bearing.

# Validation rules: teaching CONTINUUM your domain's staleness

CONTINUUM's validator asks one question: does the checkpointed state still match
the environment it was recorded against? That question catches a dataset that
moved from v3 to v4, and it propagates the answer from the dependency through
the evidence and findings to the decisions resting on them.

It cannot catch a decision that is void because the regulation it cites was
revoked. Nothing in the environment moved; the decision is nonetheless no longer
safe to act on. A domain knows staleness the environment cannot express, and
before issue #761 the only way to contribute it was to patch
`StateValidator`, which forks core recovery behaviour and makes provenance and
upgrade compatibility hard to audit.

A validation rule is the seam for that. This guide covers what a rule may do,
what it may not, and how to register one.

## What a rule is

Any object with two members:

```python
from continuum.models import ComponentValidationEntry, SemanticState, EnvironmentSnapshot


class RevokedPolicyRule:
    name = "acme:revoked_policy"

    def evaluate(
        self, state: SemanticState, environment: EnvironmentSnapshot | None = None
    ) -> list[ComponentValidationEntry]:
        ...
```

`name` namespaces every finding the rule reports, in the contract, in the JSON
payload, and in the text rendering. Use a prefix that belongs to your
integration (`acme:`, not `rule1`) so a reader can tell your finding from a
built-in one and from another integration's.

`evaluate` returns structured component-validation entries, not statuses: each
names the component, its id, a status, and a one-line detail. Return an empty
list when the rule has nothing to report.

## The trust boundary: what a rule does not receive

`evaluate` gets the projected semantic state and the current environment, both
immutable pydantic models, and nothing else. No storage handle, no event writer,
no repair plan, no clock.

That is the boundary, and it is enforced by what the seam does not pass. A rule
that cannot reach storage cannot mutate it; a rule with no event writer cannot
emit events; a rule that never sees the repair plan cannot rewrite the work it
gates. A rule that needs wall-clock time must take it as an argument from its
own caller, because a rule that reads the clock returns different findings for
identical inputs, and the conformance check below says so.

Nothing here is a sandbox. A rule is Python in your process, and a rule that
imports your storage module and uses a global handle can still write. What the
boundary guarantees is that the *interface* offers no such handle, so a rule you
did not write to be hostile does not become accidentally destructive, and a
reviewer auditing one has a small surface to read.

## Registering a rule

Registration is explicit. CONTINUUM never imports a rule from a path, a plugin
directory, or an entry point, because automatically executing a third party's
staleness logic during recovery is a decision an operator must make on purpose.

Pass rules to the engine, or register them in a `Registry`:

```python
from continuum.plugins import Registry, RevokedApprovalRule
from continuum.recovery import RecoveryEngine

rules = [RevokedPolicyRule()]

# Explicit: a list of rules, at construction or per assessment.
engine = RecoveryEngine(storage, validation_rules=rules)
decision = engine.assess(run_id, current_environment=env, validation_rules=[AnotherRule()])

# Registry-backed: every registered ValidationRule is picked up.
registry = Registry()
registry.register("revoked_policy", RevokedPolicyRule())
engine = RecoveryEngine(storage, registry=registry)
```

Per-assessment rules add to the engine's configured set rather than replacing
it. An engine with no rules behaves exactly as before: the rule path is skipped
unless at least one rule is configured, and a rule that finds nothing leaves the
report and the sealed contract byte-identical.

## How findings merge

Most cautious wins. Built-in validation runs first; rules run over the state it
already revised; and per component, the most cautious status among built-in and
every rule's finding is what the report carries.

```python
from continuum.recovery.rules import STATUS_CAUTION
```

`STATUS_CAUTION` orders the statuses ascending: `VALID` is the minimum and every
other status beats it. Two consequences:

- A rule can **raise** a component's status, from `VALID` to `INVALID`, and the
  repair plan, the contract, and the recovery mode all follow.
- A rule **cannot lower** what built-in validation or another rule already
  found. A rule reporting `VALID` for a component the validator marked `STALE`
  changes nothing. A rule cannot launder a finding, not even its own.

Merge is commutative, so the order you register rules in does not change the
report. Findings a rule reports for a component built-in validation never
examined are appended, with the rule's namespace attached.

## When a rule misbehaves

A rule that crashes, returns malformed output, carries no name, or shares a name
with another rule does not get ignored, and does not take down the assessment.
It becomes a finding of its own, against the `validation_rule` component, with a
`requires_review` status and a detail naming the rule and what it did wrong:

```
[!!] validation_rule acme:revoked_policy: requires_review - evaluate() raised
     ConnectionError: policy service unreachable [rule:acme:revoked_policy]
```

That finding withholds a clean resume and maps to a human repair step. A broken
rule escalates rather than silently widening the trust boundary. If you would
rather a temporarily-broken rule not gate your runs, do not register it: there
is no "warn but proceed" mode, because a rule that reports nothing and a rule
that is broken are otherwise indistinguishable to the caller reading the report.

## Checking your own rule: conformance

`check_validation_rule` runs the contract checks a rule must satisfy, and is
meant for your test suite, not production (it evaluates each state several
times, which is how it detects nondeterminism at all):

```python
from continuum.plugins import check_validation_rule

report = check_validation_rule(RevokedPolicyRule())
assert report.passed, report.failures
```

It checks the interface (a non-empty string `name`, an `evaluate` returning a
list of entries), determinism (two calls with equal inputs return equal
findings), read-only behaviour (the state and environment handed in are
unchanged afterwards), namespacing (no finding labelled with another rule's
name), and tolerance of a state with nothing in it.

Pass your own representative state, because a rule may be well-behaved on the
default states and broken on yours:

```python
report = check_validation_rule(RevokedPolicyRule(), states=[my_state], environment=my_env)
```

What it does **not** check is whether the rule's verdict is *true*. Determinism
guarantees the same answer twice, not that the answer is right. Review the
logic, and review what it reads: a rule that consults a live service is a rule
whose verdict can change between the assessment and the resume.

## The built-in example

`RevokedApprovalRule` (`builtin:revoked_approval`) is the worked example of the
seam, small enough to read in one screen. The validator already grades an
approval itself; this rule follows a revocation to whatever the approval
authorized, pulling a decision, finding or plan unit to `invalid` when the
approval naming it as its subject was revoked.

It is opt-in and off by default. It exists to be read and copied, not to be a
policy product: a rule author should be able to see the whole seam, from
registration to finding, without leaving one file.

## What this is not

A rule reports what it can verify from state it is given. It does not learn from
prior runs, does not infer policy from approvals it observed, and does not get
more confident over time. Rules compose by maximum caution, not by vote and not
by trust score. Building the inductive version of this, where "unsafe" is a
learned label, is a different product with a different failure mode, and the
seam here is deliberately too small to express it.

## Where rule findings appear

Every surface that carries a validation finding carries a rule's finding too,
namespaced:

- `RecoveryDecision.validation.report.statuses`, with `entry.rule` set.
- The sealed contract's `invalidated`, `verified` and `evidence` lists, as
  `component:id [rule:name]`.
- The `continuum validate --json` payload (the contract) and the text
  rendering (`continuum validate`, `continuum resume`).
- The repair plan, as the step the finding implies, and the contract's
  `required_actions` and `next_allowed_action`.

`verify_contract` still passes: rule findings are covered by the integrity hash
because they change the contract's terms, and a contract with no rule findings
seals identically to one assessed before #761.

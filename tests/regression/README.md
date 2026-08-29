# Regression & differential testing framework

Prevents us from accidentally making the detector **noisier** (more false
positives) or **blinder** (fewer real defects) while adding features. Every
finding delta is scored against fixture ground truth, so **a change that
increases findings is not automatically an improvement**.

It runs the real audit entrypoint (`audit-orchestrator`) against the 21
[adversarial fixtures](../adversarial/README.md) in hermetic fixture mode
(no network), records a full snapshot per run, and diffs two snapshots.

## What a snapshot stores

For every fixture, per run:

| Field | Content |
|---|---|
| `crawl` | crawl snapshot — pages discovered/fetched, robots, sitemaps, per-page status/canonical/noindex/depth |
| `extracted_pages` | per page: page-class, status, raw/rendered lengths, and content **fingerprints** (sha) so extraction changes are visible without storing full text |
| `findings` | every finding: `check_id`, severity, confidence, mechanism, affected URLs, evidence fingerprint |
| `recommendation` | per finding: summary, priority, effort, do-first, rank, steps fingerprint |
| `runtime_s` | wall-clock time to run the fixture |
| `request_count` | pages requested / crawled, gate page fetches, external calls |
| `scores` | overall / discoverability / engagement scores |
| `confusion` + metrics | TP / FP / FN / TN and precision / recall / FP-rate / FN-rate vs. fixture expectations |

Snapshots are **deterministic** (sorted keys, fingerprinted evidence) and
self-describing (they carry the git commit and the catalog size of the version
that produced them).

## Usage

Same fixtures, different detector code (the common inner loop):

```bash
python3 tests/regression/regression.py snapshot --label baseline
# ...make changes...
python3 tests/regression/regression.py snapshot --label candidate
python3 tests/regression/regression.py diff baseline candidate --fail-on-suspicious
```

Compare two git versions directly (each version runs its own code + fixtures via
a throwaway `git worktree`; your working tree is untouched):

```bash
python3 tests/regression/regression.py snapshot --ref HEAD~1 --label prev
python3 tests/regression/regression.py snapshot --label now
python3 tests/regression/regression.py diff prev now
```

Other:

```bash
python3 tests/regression/regression.py list                # stored snapshots
python3 tests/regression/regression.py snapshot --label x --only saas ecommerce
```

## The differential report

Written to `reports/<baseline>__vs__<candidate>.json` (machine-readable) and
printed as a summary. It contains:

- **added / removed / changed findings**, each matched by `check_id` + affected
  URLs (URL-only moves are reconciled as *changed*, not churn), and each labelled
  against fixture ground truth as `improvement` / `regression` / `neutral`.
- **severity / confidence / recommendation changes** (counts + before/after).
- **metric deltas**: precision, recall, FP-rate, FN-rate.
- **runtime & request-count changes**.
- **flags** at four levels: `suspicious`, `regression`, `warning`, `info`.

### Suspicious-regression flags (fail the build)

| code | meaning |
|---|---|
| `new_false_positive` | a finding appeared that the fixture forbids — noisier |
| `finding_on_clean_fixture` | any finding on a precision anchor (`perfect-static`, `local-business`, `docs-heavy`) |
| `precision_regression` | aggregate precision dropped |
| `fp_rate_increase` | aggregate false-positive rate rose |
| `recall_regression` / `lost_true_positive` | a real defect is no longer caught |
| `findings_up_no_quality_gain` | total findings rose with **no** precision or recall gain — the core noise guard |

`warning`-level flags (`severity_inflation`, `new_unscored_finding`,
`runtime_regression`, `request_count_increase`, `fixture_removed`) are surfaced
for review but do not by themselves mark the diff suspicious.

`diff --fail-on-suspicious` exits non-zero when any `suspicious`/`regression`
flag fires, so it can gate CI.

## Regression guard test

`tests/test_regression_framework.py` runs in the normal suite
(`python3 tests/run_tests.py`) and asserts the framework itself is correct:
snapshots are deterministic, self-diffs are `unchanged`, and injected regressions
(false positive on a clean anchor, dropped true positive, findings-up-without-
quality-gain, severity inflation) are all flagged.

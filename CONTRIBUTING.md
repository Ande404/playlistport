# Contributing

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
pytest
```

On Python 3.14, editable installs are silently broken (`site` skips `.pth` files
beginning with `_`). Prefix commands with `PYTHONPATH=src`, or install
non-editable.

## What this project values

**Evidence over assertion.** Match-quality claims should come with numbers and a
sample size. If you change scoring, report the before and after across a real
playlist set — `dryrun` writes a JSON report per run for exactly this.

**Failing loudly over degrading quietly.** The worst bugs found here were all
silent: a truncated read reported as success, a job marked complete with a failed
track, enrichment falling back to worse metadata without an error. Prefer an
exception to a plausible-looking wrong answer.

**Tests that encode real failure modes.** A test asserting that two identical
strings match is worth little. A test asserting that `Song` never auto-matches
`Song (Live at Wembley)` is worth a lot, because it pins a decision that was
expensive to get right.

## Where things live

| Path | Responsibility |
| --- | --- |
| `providers/` | Everything platform-specific. The only place a platform is named. |
| `core/normalize.py` | Title and artist cleaning, variant-tag detection |
| `core/matcher.py` | Pure scoring. No network, no provider knowledge. |
| `core/jobs.py` | fetch → match → review → write, resumable at every step |
| `db/` | Schema. The correctness guarantees come from here. |

Adding a platform: see [docs/adding-a-provider.md](docs/adding-a-provider.md).

## Schema changes

There is no migration framework — deliberately, for a single-user local
database. Additive columns go in `db/session.py:_ADDED_COLUMNS` and are applied
at startup. `create_all` only creates missing *tables*, so without this an
existing database keeps the old shape and fails on first query rather than at
startup.

Anything beyond an additive column needs a real migration story; raise an issue
first.

## Pull requests

- `pytest` passes
- New behaviour has a test that would fail without the change
- Anything touching match quality includes measured before/after numbers
- User-visible changes update the README

## Reporting match-quality problems

Include the JSON report from `dryrun` — it carries per-track score components
and the alternatives considered, which is what makes a bad match diagnosable
rather than anecdotal. Redact playlist names if you would rather not share them.

# Keeping this repo in step with the other one

This repository is a full copy of the codebase, not a thin deployment of a shared
package. That was a deliberate choice — total isolation between two people's
financial data, with nothing shared at runtime — and it has exactly one cost:

**A fix made in one repo does not reach the other.**

Every bug fixed in the cost-basis engine, every improvement to the factor
regression, every provider whose file layout changes, has to be applied twice. The
failure mode is not dramatic; it is that six months from now the two portfolios
compute a number differently and nobody notices which one is right.

This file exists so that cost stays managed rather than merely accepted.

## The two repos share history

Both descend from the same commits, so git can move changes between them
directly. Two remotes are already configured — `origin` for this repo, and a
remote named `ergi` pointing at the other one. Confirm with:

```bash
git remote -v
```

(The URLs are omitted here on purpose: an SSH remote reads as an email address to
`tools/pii_scan.py`, and adding a suppression comment to dodge that would set a
bad precedent in a file whose whole job is explaining hygiene.)

## Pulling a fix across

See what the other repo has that this one does not:

```bash
git fetch ergi
git log --oneline main..ergi/main
```

Take a specific commit:

```bash
git cherry-pick <sha>
```

Take everything, when the other repo is simply ahead and nothing here has
diverged:

```bash
git merge ergi/main
```

Both work because the histories share a base. Neither carries any configuration
or data: `config/portfolio.yaml`, `.pii-denylist`, `.streamlit/secrets.toml` and
the database are all gitignored in both repos, so a merge moves code and nothing
else.

## What must never be merged

Nothing in the tracked tree is personal, with two exceptions to watch:

- **`data/lookthrough/composition.json.gz`** is per-portfolio. It holds the
  published compositions of the funds *this* person owns, so merging the other
  repo's copy would describe the wrong funds. It is public reference data rather
  than personal data, so no harm is done by it leaking — but the X-Ray tab would
  quietly report a portfolio nobody holds. If a merge touches this file, take
  this repo's version.
- **`config/portfolio.example.yaml`** is shared and safe. The real
  `config/portfolio.yaml` is gitignored in both and cannot be merged by accident.

## Worth revisiting

At two people, copying fixes by hand is cheap. At three or four it stops being
cheap, and the right shape becomes a published `desk` package with a thin
deployment repo per person holding only config, secrets and a pinned version —
which is what "the library is the product" in the README was aiming at. The
analytics layer is already isolated enough by the import contracts to be
extracted without untangling anything.

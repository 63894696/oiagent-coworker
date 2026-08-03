# License Policy — OIagent-coworker

This document is the **operational** license policy that backs the
automated scan in `scripts/license_lint.py`. It is human-readable and
is the source of truth for *why* a license is GREEN/YELLOW/RED — the
linter only enforces the rules.

## 1. License 红线 (hard constraints)

### 1.1 RED — block-on-detect

| License | Why blocked | Tool |
|---|---|---|
| GPL-2.0, GPL-3.0 | Strong copyleft; would force OIagent-coworker itself to be GPL | `license_lint.py` |
| AGPL-3.0 | Network-copyleft; would force OIagent-coworker + any service using it to be AGPL | `license_lint.py` |
| SSPL-1.0 | Source-available, not OSI; would force OIagent to publish its entire stack | `license_lint.py` |
| Commons Clause | Blocks commercial sale; incompatible with "optional commercialization" strategy | `license_lint.py` |
| BUSL-1.1 | Time-delimited source-available; restrictions on production use | `license_lint.py` |
| Elastic-2.0 / Elastic-1.0 | Source-available with non-OSI restrictions | `license_lint.py` |
| QPL-1.0 | Strong copyleft, viral | `license_lint.py` |
| OSL-3.0 | Strong copyleft, vague scope | `license_lint.py` |

**Action when RED**: build fails (`--ci` exits 1). Replace the dependency
or remove the feature. Do NOT add a "process segregation" justification
in the linter — that decision is project-policy and must be reviewed by
a human, not silently allowed by a tool.

### 1.2 YELLOW — review-on-add

| License | When allowed | Tool |
|---|---|---|
| LGPL-2.1, LGPL-3.0 | Dynamic linking only, with a documented interface boundary | `license_lint.py` + explicit `LGPL_DYNAMIC_OK` allow-list |
| EPL-1.0, EPL-2.0 | Modular use (one module per process) | `license_lint.py` |
| MPL-2.0 | File-level copyleft is fine; do not modify MPL files in-place | `license_lint.py` |
| CDDL-1.0, CDDL-1.1 | File-level copyleft; do not modify CDDL files | `license_lint.py` |
| PSF-2.0 | Python-specific; usually fine for tooling | `license_lint.py` |

**Action when YELLOW**: linter emits a warning. The author must add a
row to the `LGPL_DYNAMIC_OK` table (or an equivalent justification file)
or the warning repeats on every CI run. The justification must point at
a concrete `process boundary` or `interface boundary` and must be
re-readable in 6 months.

### 1.3 GREEN — default

| License | Notes |
|---|---|
| MIT, MIT-0 | Default; preferred for new deps |
| BSD-2-Clause, BSD-3-Clause, 0BSD | Equivalent to MIT |
| Apache-2.0 (+ LLVM-exception) | Preferred for Rust deps |
| ISC | Equivalent to MIT |
| MPL-2.0 | Allowed but treated as YELLOW for stricter policy |
| Unlicense, CC0-1.0 | Public domain equivalents |
| CC-BY-4.0, CC-BY-SA-4.0 | Documentation-only; never applies to code |
| OFL-1.1 | Fonts only |
| Python-2.0 | PSF-style; treated as YELLOW if there's any doubt |
| Zlib, Libpng, BSL-1.0 | Permissive |
| OpenSSL | OpenSSL/SSLeay dual — YELLOW in practice (re-licensed to Apache-2.0 in 3.0) |

### 1.4 DOCUMENT — ignored in code context

| License | Notes |
|---|---|
| CC-BY-4.0 / CC-BY-SA-4.0 | Documentation-only; code is unaffected |
| CC-BY-NC-4.0 / CC-BY-NC-SA-4.0 / CC-BY-NC-ND-4.0 | NC is **not** open-source; allowed only for documentation and assets, never for code |
| OFL-1.1 | Fonts only |

These are recognized in `notices` and `LICENSE-*-asset.txt` files but
do NOT flag `license_lint.py` RED/YELLOW when found in `*.md` or `*.txt`.

## 2. Process

1. **Adding a new dependency**: run `python scripts/license_lint.py .`
   and verify the new dep is GREEN. If YELLOW, add a justification row
   and re-run. If RED, do not add.
2. **CI gate**: `python scripts/license_lint.py . --ci` runs on every
   PR. RED exits 1, blocking merge.
3. **Monthly audit**: a human reviews the regenerated
   `docs/license-report.md` and re-confirms each YELLOW row's
   justification is still accurate.
4. **License change**: if a dep's upstream re-licenses (e.g. Elastic
   re-licensing from Apache-2.0 to Elastic-2.0), the linter catches
   the new RED on the next CI run. The team must either pin the old
   version (with a security plan) or replace the dep.

## 3. Why this policy exists

The OIagent project intends to remain MIT-licensed even if it
incorporates a team or company. The two reasons:

1. **Talent inflow**: breaking protocol changes (e.g. relicensing to
   AGPL to chase a competitor) block individual contributors who would
   otherwise join. MIT preserves the original "scratch your own itch"
   energy of the upstream.
2. **Commercial clarity**: a permissive license lets downstream users
   (including any future OIagent commercial offering) use the code
   without legal escalation. AGPL/GPL would force every downstream
   user to re-publish, which kills adoption.

This policy is the operational expression of that strategy. It is
not a legal document; it is a code review checklist.

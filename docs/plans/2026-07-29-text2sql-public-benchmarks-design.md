# Text-to-SQL public benchmark design

## Scope

Evaluate the existing Text-to-SQL pipeline in this order:

1. BIRD Mini-Dev, SQLite, the 500 SELECT-only cases.
2. Spider 2.0-Lite, all available SQLite cases.

The primary run exercises the complete authenticated API and workflow rather
than calling SQL generation components directly. A failed case may be replayed
through diagnostic component entry points to attribute the failure, but replay
results do not replace the end-to-end score.

## Isolation and reproducibility

Benchmark questions, database schema, and benchmark-provided evidence are
pipeline inputs. Gold SQL and expected results are evaluator-only data and must
never be included in prompts, schema metadata, successful-SQL memory, or
diagnostic replay inputs.

Each run records:

- repository commit and dirty-state digest;
- dataset source revision and file digests;
- model/provider configuration identity;
- pipeline configuration digests;
- case ordering and random seed;
- generated SQL, terminal status, reason code, latency, and stage diagnostics.

Successful-SQL persistence is disabled for benchmark runs. Schema caches are
namespaced by benchmark and database. Case ordering is stable, and failed
diagnostic replay cannot mutate state used by later scored cases.

## Evaluation

Official execution-based evaluators are the source of truth for benchmark
accuracy. Local comparison is used for diagnostics and must preserve benchmark
semantics for ordering, nulls, numeric values, and duplicate rows.

The report includes:

- execution accuracy and executable-SQL rate;
- results by database and published difficulty;
- failure counts by pipeline stage and reason code;
- schema-linking table and column recall where derivable from gold SQL;
- latency distribution and model-call usage when observable;
- representative failure traces with gold kept outside pipeline inputs;
- confidence-ranked pipeline findings and an ordered remediation backlog.

Spider 2.0-Lite results are reported explicitly as the SQLite subset rather
than as a full Spider 2.0-Lite leaderboard score.

## Failure analysis

Every failed case is assigned to the earliest evidenced failure stage:

1. dataset or connection setup;
2. schema discovery or representation;
3. schema linking;
4. value grounding or external-evidence use;
5. SQL generation or dialect handling;
6. safety or schema validation;
7. execution;
8. result comparison.

The final bug hunt reconstructs intended behavior from benchmark specifications
and repository contracts. Findings must survive an adversarial challenge and
reach at least medium confidence. Benchmark annotation defects are reported
separately from pipeline defects.

## Verification

The harness has focused tests for parsing both datasets, oracle separation,
stable selection, result comparison, memory isolation, malformed cases, and
partial-run resume. A small end-to-end smoke run must succeed before either
full benchmark starts. Raw and summarized artifacts remain reproducible from a
single documented command.

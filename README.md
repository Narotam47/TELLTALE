# TELLTALE

Competitive intelligence pipeline tracking hiring signals across Indian fintech companies.

## Setup

```bash
uv sync --all-extras
cp .env.example .env   # edit with your credentials
```

## Usage

```bash
telltale run        # full pipeline: scrape -> classify -> signals -> brief
```

Or a stage at a time:

```bash
telltale doctor     # which providers are reachable, in quota, and usable
telltale scrape     # scrape careers pages into the database
telltale classify   # classify postings with the configured LLM
telltale signals    # compute this week's hiring signals
telltale brief      # write the weekly brief to briefs/ and the database
```

`telltale run` accepts `--skip-scrape` and `--skip-classify` to rebuild signals and
the brief from data already stored.

## Signals

`telltale/pipeline/signals.py` computes, for a week: postings opened and closed per
company per function; each company's function mix as a **share** of its own open roles
(absolute counts would let Paytm's 227 postings drown the other six); first-ever entry
into a function; seniority mix; and sector-wide totals.

Cold start is explicit. With one week of history there is no prior period, so
`fastest_growing_function` is `None`, every "first entry" is first by definition, and
the report says so in `notes`. The brief prompt is instructed to describe structure
rather than invent trends when `cold_start` is true.

## Briefs

`telltale brief` sends the signal report to the LLM and writes a one-page markdown
brief to `briefs/<week>.md` and the `weekly_briefs` table. The prompt forbids any
number not present in the report, and `audit_numbers()` verifies that afterwards
rather than trusting it — every figure in the body must appear in the signal JSON, or
it is flagged. Section bullet counts are checked too, so the page stays a page.

## Classification accuracy

**86% category accuracy and 90% seniority accuracy on 50 hand-checked samples
stratified across all 7 companies.** Both fields correct on 76%.

| | correct | n |
|---|---|---|
| function_category | 43 | 50 |
| seniority_level | 45 | 50 |
| both | 38 | 50 |

Accuracy is not uniform. Credit & Risk, Engineering - Platform & Payments,
Finance & Accounting, People & HR, Data & ML and Compliance & Regulatory were all
correct on every sampled posting. The errors concentrate in two places:

- **Product, 57% (4/7)** — leaked to Data & ML, Operations and Growth & Marketing.
- **Growth & Marketing, 70% (7/10)** — two postings went to Engineering - Platform
  & Payments.

Seniority errors are all one step wide (Mid↔Junior, Mid↔Senior, Senior→Leadership),
never a jump across the scale.

Treat the Product boundary as the known weak spot when reading a brief. Reproduce or
re-measure with:

```bash
uv run python scripts/eval_classification.py sample --out eval/sample_final.csv
# fill in correct_category / correct_seniority by hand, then:
uv run python scripts/eval_classification.py report --csv eval/sample_final.csv
```

## Automation

`.github/workflows/weekly.yml` runs the pipeline every Monday at 00:30 UTC (06:00 IST),
and on demand from the Actions tab. It scrapes, classifies, computes signals, writes a
brief, checkpoints the database, and commits `telltale.db` and `briefs/` back to the
repo with `[skip ci]` so the push does not re-trigger the workflow. Logs upload as an
artifact for 14 days; a failure opens an issue.

`make pipeline` is the local equivalent.

**The database is committed on purpose.** `.gitignore` excludes `*.db` but re-includes
`telltale.db`, so a clone reproduces every brief. The WAL sidecars stay ignored — a
checkpoint folds them into the `.db` before each commit, and CI verifies the copied
file alone is readable. Expect the repo to grow by roughly the database size each week
that postings change.

**Classification degrades rather than fails.** It is the only stage needing an LLM, and
free tiers are not dependable in CI (Groq: 200k tokens/day, about one full pass;
Gemini: 20 requests/day per model, about one pass at batch 20). If no key is set the
stage is skipped with a warning; if the provider errors mid-run the step is marked
`continue-on-error`. Either way the brief falls back to `telltale brief --snapshot`, a
deterministic figures-only summary with no interpretation. The scrape is the
irreplaceable part — postings vanish once taken down — so a run that cannot classify is
still worth completing.

## LLM providers

Selected with `LLM_PROVIDER` in `.env` (`groq` | `gemini` | `ollama`). Check any of
them with `telltale doctor` (add `--deep` to detect a spent daily token budget, which
providers do not expose in headers).

| provider | pinned model | binding limit | fits a 367-posting pass? |
|---|---|---|---|
| groq | `openai/gpt-oss-120b` | 8k tokens/min **and 200k tokens/day** | one pass (~175–205k) consumes essentially the whole day |
| gemini | `gemini-3.6-flash` | 5 requests/min **and 20 requests/day per model** | one pass only, at batch 20 (19 requests); two passes need two days |

The daily caps are the ones that bite. Gemini's 20-requests-per-day ceiling is *per
model*, so switching models buys a fresh allowance — but never do that mid-experiment,
since the model must stay constant for results to be comparable.

API keys are opaque strings. Gemini keys come in both a legacy `AIza` and a newer
`AQ.` form; nothing validates key shape, since only the API can say whether a key
works.

Model notes: `gemini-2.0-flash` and `gemini-2.5-flash` are retired and return 404.
`gemini-3.7-flash` returned persistent `503 UNAVAILABLE` under real load. The default
is a pinned concrete model rather than a `-latest` alias, which would drift and
silently invalidate cross-version comparisons.

## Prompt versions

`VERSION_PROFILES` in `telltale/llm/client.py` pins the input window each version runs
with, so a version stays reproducible after the fact:

| version | window | chars | batch | categories |
|---|---|---|---|---|
| v1 | raw | 600 | 20 | 13 in the prompt; **stored rows migrated to 12** |
| v2 | raw | 600 | 10 | 13 |
| v3 | boilerplate-stripped | 400 | 10 | **12** (the merged taxonomy) |

**v3 is the shipping version** — 367 rows, the merged 12-category taxonomy from
`docs/taxonomy.md`. The pipeline, the workflow and the dashboard all read v3.

`v1` is retained as history: its 367 rows were migrated in place to 12 categories by
SQL, but `classify_v1.txt` is left unedited as a record and would emit 13-category
labels if re-run. Do not classify at v1 again.

v3 was completed across three models after free-tier daily caps were hit mid-run
(`groq/openai/gpt-oss-120b` 170 rows, `gemini-3.1-flash-lite` 177,
`gemini-3.6-flash` 20). `model_name` is stored per row, so a slice by model is
available — worth remembering given the ~9% category disagreement measured between
model families on identical input.

A `-suffix` selects the same prompt and window profile while storing under its own
label: `--prompt-version v3-gemini` uses the v3 prompt but records rows as
`v3-gemini`.

## Batch size and classification stability

`LLM_BATCH_SIZE` defaults to **10**. It was 20, but classifying the same 40 postings
at both sizes (same prompt, same model) produced:

| | differs |
|---|---|
| function_category | 4/40 (10.0%) |
| seniority_level | 8/40 (20.0%) |
| either field | 12/40 (30.0%) |

Labels should not depend on which other postings share a request. The disagreements
clustered on genuinely ambiguous roles (collections → Operations vs Credit & Risk;
"Engineering Manager" → Senior vs Leadership), and larger batches appear to push the
model toward answers that are consistent *within* a batch rather than correct on their
own. `classify_v2.txt` addresses the ambiguity directly; the smaller batch removes the
composition effect.

**Token cost.** The prompt template is resent with every request, so halving the batch
doubles that overhead. For a full 367-posting run with the v2 template (~1,730 tokens):

| batch size | requests | template overhead |
|---|---|---|
| 20 | 19 | ~33k tokens |
| 10 | 37 | ~64k tokens |

That is roughly **+31k prompt tokens per full run** (posting payload tokens are
unchanged). Wall clock grows similarly — a full run takes ~12–13 min rather than ~10.
Raise `LLM_BATCH_SIZE` if you are cost-constrained and can tolerate label churn
between runs.

## Dashboard

```bash
streamlit run dashboard/app.py
```

## Tests

```bash
pytest
```

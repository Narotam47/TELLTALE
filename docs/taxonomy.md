# Function taxonomy

Twelve closed categories. The classifier may not invent one; `Other` is the escape
hatch for genuinely ambiguous postings.

| category | covers |
|---|---|
| Engineering - Platform & Payments | all non-mobile engineering: settlement, reconciliation, UPI, card rails, gateways, ledgers, PSP/bank integrations, plus CI/CD, cloud, observability, SRE, databases, internal tooling, data platform |
| Engineering - Mobile | Android, iOS, React Native, mobile SDK |
| Credit & Risk | underwriting, collections, lending products, fraud, risk modelling |
| Compliance & Regulatory | regulatory tracking, filings, audit, legal, secretarial |
| Data & ML | building models, pipelines, and data systems (not consuming reports) |
| Product | roadmap, requirements, prioritisation, specs |
| Design | product, visual, and UX design |
| Growth & Marketing | sales, business development, partnerships, account management, campaigns, brand, ad sales |
| Operations | support, back-office processing, field operations, facilities, service delivery |
| Finance & Accounting | company financials, FP&A, controllership, reconciliation as a finance process |
| People & HR | recruiting, HR operations, people partnering |
| Other | genuinely ambiguous; rationale must say why |

## The Payments Infrastructure merge

**What changed.** `Payments Infrastructure` was removed and folded into
`Engineering - Platform`, renamed `Engineering - Platform & Payments`. Thirteen
categories became twelve.

**Why.** The category was designed to be the most strategically interesting signal in
the dataset: which competitors are staffing up on payment rails. It never worked. It
held 3–4 postings out of ~367 under every configuration we tried:

| configuration | Payments Infrastructure |
|---|---|
| v1 — original prompt, raw 600-char window, gpt-oss-120b | 4 |
| v2 — rewritten prompt with an explicit payments-vs-platform rule and three worked examples, gpt-oss-120b | 4 |
| v2 window, gemini-3.6-flash (different model family, identical prompt and window) | 3 |

Across the 180 postings classified by both models, the two disagreed in both
directions (2 postings moved into the category, 3 moved out) and the total barely
shifted. For Razorpay, PhonePe and Paytm — three companies whose core business *is*
payment rails — the category never exceeded 4 postings combined.

Three explanations were tested and eliminated:

1. **Prompt wording.** v2 stated the rule explicitly ("backend roles touching
   settlement, reconciliation, UPI, card rails, gateway, ledger, or PSP integration
   are Payments Infrastructure, NOT Engineering - Platform") with worked examples
   drawn from real titles in this database. No effect.
2. **Model capability.** A different model family on the identical window produced
   the same near-empty category.
3. **Input truncation.** The model saw only the first 600 characters, which for 63%
   of postings was company boilerplate. A boilerplate stripper was built and does fix
   the input (windows with 0% role content: 148 → 4). But inspection of the *full*
   PhonePe engineering descriptions showed the payments vocabulary is not there
   either — these read as generic backend roles that happen to be at a payments
   company.

**The conclusion.** The boundary does not exist in the source text. These companies
do not write "settlement" or "UPI rails" in engineering job descriptions; they write
"backend systems at scale". A category that cannot be populated from the available
evidence is not a useful category, and a near-empty bucket in a weekly brief is worse
than no bucket — it invites reading noise as signal.

**What was NOT done.** The existing v1 classifications were not re-run. Both source
categories map unambiguously onto the merged one, so the merge was applied as a SQL
`UPDATE` over the 367 v1 rows (38 `Engineering - Platform` + 4 `Payments
Infrastructure` = 42). Reclassifying would have spent a day's provider quota to
reproduce a result already determined by the mapping.

**Cost of the merge.** We lose the ability to answer "is competitor X staffing
payment rails specifically?" from the function category alone. If that question
matters later, the better route is a *tag* derived from description keywords
alongside the category, not a sibling category the classifier cannot separate.

## Seniority levels

Six, also closed: `Intern`, `Junior`, `Mid`, `Senior`, `Staff/Principal`,
`Leadership`. `Leadership` is reserved for roles owning an org, function, or multiple
teams — an Engineering Manager of a single team is `Senior`.

## Version history

| version | prompt | window | notes |
|---|---|---|---|
| v1 | `classify_v1.txt` | raw 600, batch 20 | 13 categories; **migrated in place to 12** |
| v2 | `classify_v2.txt` | raw 600, batch 10 | 13 categories; adds worked examples, standalone-rationale rule |
| v3 | `classify_v3.txt` | stripped 400, batch 10 | 12 categories; v2 plus the merge |

v1 rows now carry 12 categories despite v1's prompt naming 13. The prompt file is
left unedited as a historical record; `docs/taxonomy.md` and the `v1` rows in
`posting_classifications` are the authority on what the stored labels mean.

# Retrieval Corpus (Week 2)

The corpus the RAG layer (compensation C1) indexes. It is fully synthetic and reproducible from seed 42.

```bash
python -m data.generate_corpus            # writes data/corpus/ (about 6 MB)
python -m data.generate_corpus --markdown # also exports data/corpus/docs/<type>/<id>.md for browsing
python -m data.validate_corpus            # counts, gold alignment, leakage, hygiene, determinism
```

## Contents (2000 docs)

| Set | Count | How it is built | IDs |
| :--- | ---: | :--- | :--- |
| Service runbooks | 300 | 15 services x 10 failure modes x {prod, staging} | `RB-<svc>-<mode>-<env>`, steps cite as `RUNBOOK:<svc>:<n>` |
| IT SOPs | 80 | 20 categories x 4 subjects | `SOP-001`... |
| Dependency notes | 50 | 15 service maps + 34 caller->callee edges + 1 shared flagd note | `DEP-001`... |
| Poisoned / distractor | 30 | see below; they look like ordinary docs | same ID schemes as the sets they imitate |
| Past postmortems | 1540 | 7-section template from SPEC section 4; fills the corpus to `--total` | `PM:001`... |

Services: frontend, checkout, payment, cart, product-catalog, recommendation, ad, shipping, quote, currency, email,
fraud-detection, accounting, kafka, image-provider.
Failure modes: flag_fault, dependency_unavailable, memory_oom, cpu_saturation, bad_deploy, tls_expiry,
config_drift, pool_exhaustion, disk_full, dns_failure.

## Gold alignment with the Week 1 eval set

`data/generate_corpus.py` imports `OTEL_FAULTS` from `data/generate_data.py`, so the corpus can't drift from the bundles:

- `RB-<svc>-flag_fault-{prod,staging}` contains the gold `missed_step` text at exactly the gold step number
  (for example, step 3 of `RB-payment-flag_fault-prod` is the `RUNBOOK:payment:3` step).
- `PM:001`-`PM:006` are the gold similar incidents. Each gold flag (for example `paymentServiceFailure`) appears in
  no other clean postmortem. Other postmortems for the same services use sibling flags (`paymentServiceUnreachable`)
  and nearby failure modes (`Connection refused to payment-db:5432` from a pool or network cause) as hard negatives.

## Poisoned / distractor set (30)

| Type | n | What it tests |
| :--- | ---: | :--- |
| prompt_injection | 8 | Instructions embedded in runbook addenda, postmortems and dependency notes (override root cause, auto-approve DRAFT tickets, fake citations, prompt exfiltration). Includes the exact `SYSTEM OVERRIDE ... DNS_RESOLVED_NO_ACTION_NEEDED` string the `injection_doc` eval uses. |
| stale_runbook | 6 | 2023 copies of each gold runbook where the gold step is replaced by "restart pods directly" (the action the bundles' CHAT:1 takes). |
| conflicting_step_number | 4 | v2 drafts where the gold step text sits at step k+1, so a citation of `RUNBOOK:<svc>:k+1` is wrong. |
| blame_laden_postmortem | 4 | Postmortems that name fictional people with forbidden lexicon. The drafter must not copy them. |
| pii_secret_leak | 4 | Fake emails (example.com), IPs (10.x, 192.0.2.x) and tokens. Exercises the redaction requirement. |
| off_topic_lookalike | 4 | Lexical overlap only ("Vendor Payment Processing", "Office Supplies Cart", a Kafka reading group). |

## Files

- `data/corpus/corpus.jsonl`: one doc per line with `doc_id, doc_type, title, service, services, env, failure_mode, date, cite_as, text`, plus `steps` for runbooks and SOPs. **This is the only file to index.**
- `data/corpus/labels/doc_labels.jsonl`: ground truth (`set`, `gold_for`, `poison_type`, `expected_behavior`, `shadows`). **Never index or retrieve from `labels/`.** The corpus itself carries no label fields or tell-tale wording (the validator checks this).
- `data/corpus/manifest.json`: seed, counts and sha256 of both files. A changed hash means the corpus changed.

Each section draws from its own RNG (`random.Random("42:<section>")`), so resizing one section (for example `--n-postmortems 500`) leaves the others unchanged.

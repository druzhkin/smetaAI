# Governed logistics, mobilisation, and contract finance

## Purpose

TenderGuard does not estimate logistics, mobilisation, or financing as an
invented percentage of direct cost. These categories use typed, version-bound
commercial cost models:

- `LOGISTICS`: capacity-constrained transport legs, handling, storage, and
  ancillary customs/permit/escort/insurance items;
- `MOBILISATION`: outbound/return plant moves, personnel travel,
  accommodation, temporary facilities, setup/dismantling, utilities, and
  other explicitly evidenced components;
- `CONTRACT_FINANCE`: dated signed cash flows, piecewise funding-rate periods,
  and dated guarantee notionals/fees.

Every result targets one current verified BoQ component whose basis is
`DERIVED_MODEL` and whose quantity is an evidenced lump-sum `1`. The model
total is therefore the exact atomic unit rate; it is not spread across an
uncontrolled quantity.

## Controlled policy

The project must bind an approved controlled version with kind and purpose
`commercial_cost_model`. Its `policy` object supplies, without system
defaults:

- currency;
- line and total rounding;
- rounding mode;
- independent tolerance;
- day-count convention (`360`, `365`, or `366`);
- methodology-owned maximum finance horizon and component count, which bound
  the independent daily validator's resource use;
- whether a zero total is permitted;
- model kinds required for the project;
- required logistics sections;
- required mobilisation component kinds;
- required cash-flow kinds;
- contract term kinds that a finance model must trace.

Financial thresholds are not invented by the application. Every model also
requires a configured approval-policy rule for `LOGISTICS_MODEL`,
`MOBILISATION_MODEL`, or `CONTRACT_FINANCE_MODEL`.

## Evidence contract

All referenced observations must be:

- present in the same project;
- `VERIFIED`;
- attached to a document revision in the current confirmed document set;
- immutable in production PostgreSQL;
- able to reproduce the exact model values.

An observation exposes one or more machine-readable entries:

```json
{
  "commercial_cost_bases": [
    {
      "model_kind": "LOGISTICS",
      "component_id": "factory-to-site",
      "values": {
        "distance_km": "100",
        "charged_distance_factor": "2"
      }
    }
  ]
}
```

The service joins the value maps from only the observation IDs declared for
that component and compares canonical values. A mismatched distance, capacity,
rate, date, amount, currency, term reference, or other formula input blocks
the proposal. Merely attaching a plausible observation does not satisfy
lineage.

Monetary basis observations remain subject to the price-normalisation and
source-qualification contour. The commercial model does not turn an
unqualified quote into a verified price.

## Calculation and independent validation

Primary logistics calculation derives the maximum required trip count from
each supplied mass, volume, and unit-capacity constraint, then prices fixed
trip, charged vehicle-distance, toll, handling, storage, and ancillary
components.

The independent path derives ceiling trip counts with a different algorithm
and separately re-aggregates rounded components.

Primary contract-finance calculation sweeps dated cash-flow and funding-rate
boundaries. Interest is charged only while cumulative cash is negative.
Guarantee fees use their exact notionals and active dates. The independent
path performs a day-by-day cash-balance and fee calculation. A negative
balance not covered by exactly one funding-rate period blocks calculation.

Both totals must agree within the methodology-owned tolerance. All arithmetic
uses `Decimal`; line and total rounding are policy-bound.

## Approval and immutability

A passing proposal enters `REVIEW_REQUIRED` and receives a mandatory expert
task. A blocked calculation or missing approval rule is persisted as
`BLOCKED`; it cannot be finalized.

A rejected or changes-requested approval keeps the latest model unresolved.
When a corrected proposal is submitted, the rejected revision is first
settled as immutable `BLOCKED`; it is never overwritten or silently reused.

Finalization:

1. reloads the current document set, BoQ target, observations, terms, and
   controlled policy;
2. recalculates both paths from the stored typed input;
3. verifies input/output hashes and exact totals;
4. requires all approval tasks to have approved records from actors other than
   the model author;
5. supersedes the prior current result for the same BoQ component;
6. marks the new result `VALIDATED`.

PostgreSQL permits only `REVIEW_REQUIRED -> VALIDATED` and current-to-
superseded transitions. It rejects payload, total, hash, policy, target,
approval-task, update, and delete tampering. Downgrade refuses to remove the
table once model evidence exists.

## Main calculation and contract linkage

An `AtomicCostInput` with `derived_cost_model_id` is accepted only when the
referenced model is:

- current and `VALIDATED`;
- for the same project, line, semantic key, category, unit, rate, and currency.

Snapshot lineage expands the model, controlled policy, dual results,
observations, document locations, approval tasks/records, author, and
finalizer.

A non-zero contract cost impact must reference the current validated
`CONTRACT_FINANCE` model and reproduce its target, amount, and currency.
Advance, payment deferral, retention, and guarantee conditions can therefore
be traced into dated execution cost instead of remaining detached prose.

Uncertain indexation, exchange-rate movement, penalties, or schedule outcomes
must remain explicit risk/scenario inputs. A deterministic finance model must
not silently replace uncertainty with a guessed rate.

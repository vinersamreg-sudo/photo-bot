# Economics

## Product unit

One paid package costs 49 ₽ and grants two edits plus one original.

## Measured production order

The latest completed read-only audit recorded one production order:

| Metric | Value | Evidence status |
|---|---:|---|
| Customer payment | 49.00 ₽ | measured |
| Robokassa commission | 1.91 ₽ | measured |
| Net receipt after Robokassa | 47.09 ₽ | measured |
| OpenAI image requests attributed | 1 | measured |
| OpenAI cost | $0.07 | measured dashboard/audit |
| Internal RUB reserve per request | 10.00 ₽ | estimate |
| Contribution before unknown costs | 37.09 ₽ | estimate using reserve |
| Contribution margin | 75.7% | estimate using reserve |

The estimated contribution is not accounting profit.

## Not yet determined reliably

- historical USD/RUB rate for the exact provider charge;
- tax per order;
- allocated VPS, storage and network cost;
- support time and refund/chargeback reserve;
- unused edit/original liability;
- blended cost of free users and failed/repeated provider work.

Do not invent these values. Label every update as measured, derived or estimated.

## Required operating metrics

- paid orders and net payout;
- provider requests per paid package and per public user;
- failed/no-debit request rate;
- correction/repeat usage;
- original entitlement consumption;
- refund/support cost;
- storage growth and retention cleanup;
- actual OpenAI billing reconciled to internal attempt estimates.

Update this document only after a new measured audit or an explicit pricing model
change. Historical calculations are preserved in `docs/archive/`.

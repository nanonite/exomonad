# TL shadow divergence report: `replay-fixture`

This committed pair is the deterministic replay shape for M5.5. A live run
writes its report below the temporary E2E work directory.

## Counts

| bucket | count |
|---|---:|
| MATCH | 0 |
| DIVERGENT | 3 |
| EXTRA | 1 |
| MISSING | 0 |

The fixture deliberately preserves both sides, including the fan-out call,
so replay tests cannot hide an action by dropping noisy or unmatched rows.

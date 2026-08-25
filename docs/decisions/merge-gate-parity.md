# Merge gate parity

The direct-leaf and aggregate merge paths must enforce the same canonical
review predicates before issuing merge_pr. Compare evidence is an additional
precondition and is not a server-side atomic compare-and-swap; the boundary is
defined by the merge comparison decision in merge-compare-boundary.md.

| verify_review check | Aggregate path | Direct-leaf path | Enforcement form |
| --- | --- | --- | --- |
| Approved verdict | _integrate_one_candidate calls verify_review | _direct_merge_evidence calls verify_review | Pre-merge gate |
| Reviewed head equals live head | verify_review | verify_review | Pre-merge gate |
| Reviewed patch digest equals live digest | verify_review | verify_review | Pre-merge gate |
| CI is success or neutral | verify_review | verify_review plus live watcher CI | Pre-merge gate |
| Freshness window | Not supplied by the aggregate caller | Loaded when review_policy_path is configured | Explicit optional predicate |
| Optional project policy predicate | No predicate supplied | No predicate supplied | Not an implicit gate |
| Base/head/patch/tree/CI compare evidence | Required before aggregate integration | Required before direct journaled merge | Effect arguments; fail closed |
| Authoritative merged observation | Required before aggregate adoption | Required after direct merge request | Watcher snapshot; response alone is insufficient |

The direct path opens the integrity reconciliation gate when a successful
watcher response omits compare evidence. Review mismatches remain ordinary
merge refusals so a changed head, stale verdict, failed CI, or patch mismatch
cannot be misclassified as an infrastructure incident.


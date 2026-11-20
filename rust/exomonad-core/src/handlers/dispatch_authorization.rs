//! Resolution of one logical resume's durable dispatch declarations.
//!
//! A blocked-leaf resume has one stable logical identity, the controller's
//! `blocked-resume:` intent id, derived from the owner, issue, predecessor,
//! branch, worktree and worktree fingerprint. That identity deliberately
//! survives a reauthorization under a new role configuration, because the
//! owner, predecessor and worktree are unchanged.
//!
//! The dispatch *authorization* is a different thing: caller, scope, role,
//! harness, agent type, model, effort, configuration/policy/manifest
//! fingerprints and route fingerprint. A reauthorized retry keeps the logical
//! intent id and writes a fresh `tl.dispatch_intended` declaration under the new
//! authorization. The host must therefore select exactly the declaration that
//! matches the request's full authorization tuple instead of searching by
//! intent id alone, and it must not silently treat an unresolved competing
//! declaration as history.
//!
//! Full-tuple matching is not enough on its own. Every other declaration under
//! the same intent id must be proven to have had no launch effect by durable
//! evidence: either the owner invocation is still the exact predecessor named by
//! the request and no launch row exists for the intent, or the owner invocation
//! is the single linked successor of that predecessor, its stored role route is
//! this authorization, and exactly one launch row names that successor. The
//! proof is derived from the ledger and the owner registry, never from the
//! caller's claim about why an earlier attempt failed.

use crate::effects::{EffectError, EffectResult};
use crate::services::agent_control::{InvocationRecord, InvocationTrigger, RoleRouteProvenance};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// One `tl.dispatch_intended` row, reduced to the fields resolution reads.
pub(crate) struct DispatchDeclaration<'a> {
    pub run_seq: Option<u64>,
    pub agent_id: Option<&'a str>,
    pub data: &'a Value,
}

/// The full authorization tuple a dispatch declaration must reproduce.
#[derive(Clone, Copy)]
pub(crate) struct ExpectedDispatchAuthorization<'a> {
    pub intent_id: &'a str,
    pub caller: &'a str,
    pub scope_id: &'a str,
    pub role: &'a str,
    pub harness: &'a str,
    pub agent_type: &'a str,
    pub model: Option<&'a str>,
    pub effort: Option<&'a str>,
    pub configuration_fingerprint: &'a str,
    pub policy_fingerprint: Option<&'a str>,
    pub manifest_digest: Option<&'a str>,
    pub route_fingerprint: &'a str,
}

/// The selected declaration and the historical declarations it supersedes.
pub(crate) struct DispatchAuthorizationResolution {
    pub selected_run_seq: Option<u64>,
    pub superseded_run_seqs: Vec<u64>,
}

/// Durable owner evidence that bounds which declaration could have launched.
pub(crate) struct DispatchNoEffectProof<'a> {
    pub expected_invocation_id: &'a str,
    pub invocation: &'a InvocationRecord,
}

#[derive(Clone, Debug, PartialEq)]
struct ObservedIdentity {
    intent_id: String,
    agent_id: Option<String>,
    boundary: Option<String>,
    scope_id: Option<String>,
    role: Option<String>,
    harness: Option<String>,
    agent_type: Option<String>,
    model: Option<String>,
    effort: Option<String>,
    configuration_fingerprint: Option<String>,
    policy_fingerprint: Option<String>,
    manifest_digest: Option<String>,
    route_fingerprint: Option<String>,
}

impl ObservedIdentity {
    fn read(declaration: &DispatchDeclaration<'_>) -> Self {
        let text = |key: &str| {
            declaration
                .data
                .get(key)
                .and_then(Value::as_str)
                .map(str::to_string)
        };
        Self {
            intent_id: text("intent_id").unwrap_or_default(),
            agent_id: declaration.agent_id.map(str::to_string),
            boundary: text("boundary"),
            scope_id: text("slice_id"),
            role: text("role"),
            harness: text("harness"),
            agent_type: text("agent_type"),
            model: text("model"),
            effort: text("effort"),
            configuration_fingerprint: text("route_configuration_fingerprint"),
            policy_fingerprint: text("route_policy_fingerprint"),
            manifest_digest: text("route_manifest_digest"),
            route_fingerprint: text("route_fingerprint"),
        }
    }

    fn expected(expected: &ExpectedDispatchAuthorization<'_>) -> Self {
        let owned = |value: Option<&str>| value.map(str::to_string);
        Self {
            intent_id: expected.intent_id.to_string(),
            agent_id: Some(expected.caller.to_string()),
            boundary: Some("dispatch_intended".to_string()),
            scope_id: Some(expected.scope_id.to_string()),
            role: Some(expected.role.to_string()),
            harness: Some(expected.harness.to_string()),
            agent_type: Some(expected.agent_type.to_string()),
            model: owned(expected.model),
            effort: owned(expected.effort),
            configuration_fingerprint: Some(expected.configuration_fingerprint.to_string()),
            policy_fingerprint: owned(expected.policy_fingerprint),
            manifest_digest: owned(expected.manifest_digest),
            route_fingerprint: Some(expected.route_fingerprint.to_string()),
        }
    }

    fn safe_fields(&self) -> String {
        format!(
            "scope={:?} role={:?} harness={:?} agent_type={:?} model={:?} effort={:?} \
             configuration_fingerprint={:?} route_fingerprint={:?}",
            self.scope_id,
            self.role,
            self.harness,
            self.agent_type,
            self.model,
            self.effort,
            self.configuration_fingerprint,
            self.route_fingerprint,
        )
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum LaunchState {
    /// The predecessor is still the current invocation and no launch row exists.
    NoLaunch,
    /// The single linked successor of the predecessor is the current invocation.
    SingleLaunch,
}

fn sequence_label(run_seq: Option<u64>) -> String {
    run_seq.map_or_else(|| "unsequenced".to_string(), |seq| seq.to_string())
}

fn sequence_list(sequences: impl IntoIterator<Item = Option<u64>>) -> String {
    sequences
        .into_iter()
        .map(sequence_label)
        .collect::<Vec<_>>()
        .join(", ")
}

/// Resolve exactly one declaration for the request's full authorization tuple.
///
/// `launch_rows` are `agent.spawned` rows that name this intent id. They are
/// read from the same ledger snapshot as the declarations and are what make the
/// no-effect proof durable rather than a caller assertion.
pub(crate) fn resolve_dispatch_declaration(
    declarations: &[DispatchDeclaration<'_>],
    launch_rows: &[DispatchDeclaration<'_>],
    expected: ExpectedDispatchAuthorization<'_>,
    no_effect: Option<&DispatchNoEffectProof<'_>>,
) -> EffectResult<DispatchAuthorizationResolution> {
    if declarations.is_empty() {
        return Err(EffectError::invalid_input(
            "role route dispatch authorization is missing or ambiguous",
        ));
    }
    let wanted = ObservedIdentity::expected(&expected);
    let mut matching = Vec::new();
    let mut others = Vec::new();
    for declaration in declarations {
        if ObservedIdentity::read(declaration) == wanted {
            matching.push(declaration);
        } else {
            others.push(declaration);
        }
    }
    if matching.is_empty() {
        return Err(EffectError::invalid_input(format!(
            "role route dispatch authorization does not match any durable declaration \
             (declarations at run_seq {}; request {})",
            sequence_list(others.iter().map(|declaration| declaration.run_seq)),
            wanted.safe_fields()
        )));
    }
    let selected = select_single_observation(&matching, &wanted)?;
    if others.is_empty() {
        return Ok(DispatchAuthorizationResolution {
            selected_run_seq: selected.run_seq,
            superseded_run_seqs: Vec::new(),
        });
    }
    let proof = no_effect.ok_or_else(|| {
        EffectError::invalid_input(format!(
            "earlier dispatch declarations at run_seq {} cannot be released without \
             verified no-effect evidence",
            sequence_list(others.iter().map(|declaration| declaration.run_seq))
        ))
    })?;
    let selected_seq = selected.run_seq.ok_or_else(|| {
        EffectError::invalid_input(
            "the selected dispatch declaration has no durable ledger sequence",
        )
    })?;
    let launch_state = launch_state(proof, &expected, launch_rows)?;
    let mut superseded = Vec::new();
    for declaration in others.iter().copied() {
        let seq = declaration.run_seq.ok_or_else(|| {
            EffectError::invalid_input(
                "a historical dispatch declaration has no durable ledger sequence",
            )
        })?;
        if seq >= selected_seq {
            return Err(EffectError::invalid_input(format!(
                "dispatch declarations at run_seq {} compete with the selected declaration at \
                 run_seq {selected_seq}",
                sequence_list(others.iter().map(|declaration| declaration.run_seq))
            )));
        }
        verify_no_launch_effect(declaration, launch_state, proof, launch_rows, &expected)?;
        superseded.push(seq);
    }
    superseded.sort_unstable();
    Ok(DispatchAuthorizationResolution {
        selected_run_seq: selected.run_seq,
        superseded_run_seqs: superseded,
    })
}

/// Choose one observation among declarations that match the authorization.
///
/// Repeated observations of the same declaration are idempotent only when they
/// agree on every identity *and* observation field. A second observation with a
/// different start time, attempt, generation or epoch is a competing declaration
/// for the same authorization and must not be collapsed.
fn select_single_observation<'a>(
    matching: &[&'a DispatchDeclaration<'a>],
    wanted: &ObservedIdentity,
) -> EffectResult<&'a DispatchDeclaration<'a>> {
    let first = matching[0];
    if matching.len() == 1 {
        return Ok(first);
    }
    let equivalent = matching.iter().all(|candidate| {
        same_observation(first, candidate) && ObservedIdentity::read(candidate) == *wanted
    });
    if !equivalent {
        return Err(EffectError::invalid_input(format!(
            "role route dispatch authorization has competing declarations at run_seq {}",
            sequence_list(matching.iter().map(|declaration| declaration.run_seq))
        )));
    }
    Ok(matching
        .iter()
        .min_by_key(|declaration| declaration.run_seq.unwrap_or(u64::MAX))
        .copied()
        .unwrap_or(first))
}

fn same_observation(left: &DispatchDeclaration<'_>, right: &DispatchDeclaration<'_>) -> bool {
    const FIELDS: [&str; 4] = [
        "started_at",
        "attempt",
        "dispatch_generation",
        "controller_epoch",
    ];
    FIELDS
        .iter()
        .all(|field| left.data.get(*field) == right.data.get(*field))
}

fn launch_state(
    proof: &DispatchNoEffectProof<'_>,
    expected: &ExpectedDispatchAuthorization<'_>,
    launch_rows: &[DispatchDeclaration<'_>],
) -> EffectResult<LaunchState> {
    let invocation = proof.invocation;
    if invocation.invocation_id == proof.expected_invocation_id {
        if invocation.is_live() {
            return Err(EffectError::invalid_input(
                "blocked owner invocation is still live; refusing duplicate resume",
            ));
        }
        if invocation.pr_number.is_some() {
            return Err(EffectError::invalid_input(
                "blocked owner already has PR context; use resume_pr",
            ));
        }
        if !launch_rows.is_empty() {
            return Err(EffectError::invalid_input(format!(
                "dispatch intent has a launch row at run_seq {} while the named predecessor is \
                 still current",
                sequence_list(launch_rows.iter().map(|declaration| declaration.run_seq))
            )));
        }
        return Ok(LaunchState::NoLaunch);
    }
    let linked_successor = invocation.prior_invocation_id.as_deref()
        == Some(proof.expected_invocation_id)
        && invocation.trigger == InvocationTrigger::ResumeBlocked
        && invocation_role_route_matches(invocation.role_route.as_ref(), expected);
    if !linked_successor {
        return Err(EffectError::invalid_input(
            "blocked owner invocation is neither the named predecessor nor its linked \
             blocked-resume successor; its launch outcome is unresolved",
        ));
    }
    let [launch] = launch_rows else {
        return Err(EffectError::invalid_input(format!(
            "linked blocked-resume successor has {} launch rows for its intent; refusing to \
             reconcile",
            launch_rows.len()
        )));
    };
    let launch_invocation = launch.data.get("invocation_id").and_then(Value::as_str);
    if launch_invocation != Some(invocation.invocation_id.as_str()) {
        return Err(EffectError::invalid_input(
            "linked blocked-resume successor does not match its durable launch row",
        ));
    }
    Ok(LaunchState::SingleLaunch)
}

fn verify_no_launch_effect(
    declaration: &DispatchDeclaration<'_>,
    state: LaunchState,
    proof: &DispatchNoEffectProof<'_>,
    launch_rows: &[DispatchDeclaration<'_>],
    expected: &ExpectedDispatchAuthorization<'_>,
) -> EffectResult<()> {
    if declaration.agent_id != Some(expected.caller) {
        return Err(EffectError::invalid_input(format!(
            "historical dispatch declaration at run_seq {} is not attributed to the requesting \
             caller",
            sequence_label(declaration.run_seq)
        )));
    }
    match state {
        LaunchState::NoLaunch => Ok(()),
        LaunchState::SingleLaunch => {
            let launch_invocation = launch_rows[0]
                .data
                .get("invocation_id")
                .and_then(Value::as_str);
            if launch_invocation != Some(proof.invocation.invocation_id.as_str()) {
                return Err(EffectError::invalid_input(format!(
                    "historical declaration at run_seq {} is not superseded by the single \
                     linked launch",
                    sequence_label(declaration.run_seq)
                )));
            }
            Ok(())
        }
    }
}

fn invocation_role_route_matches(
    observed: Option<&RoleRouteProvenance>,
    expected: &ExpectedDispatchAuthorization<'_>,
) -> bool {
    let Some(observed) = observed else {
        return false;
    };
    let text = |value: Option<&str>| value.map(str::to_string);
    observed.role == expected.role
        && observed.harness == expected.harness
        && observed.agent_type.suffix() == expected.agent_type
        && text(observed.model.as_deref()) == text(expected.model)
        && text(observed.effort.as_deref()) == text(expected.effort)
        && observed.configuration_fingerprint == expected.configuration_fingerprint
        && text(observed.policy_fingerprint.as_deref()) == text(expected.policy_fingerprint)
        && text(observed.manifest_digest.as_deref()) == text(expected.manifest_digest)
        && observed.route_fingerprint == expected.route_fingerprint
        && observed.dispatch_intent_id == expected.intent_id
        && observed.scope_id == expected.scope_id
}

/// Stable audit identity for one accepted supersession resolution.
pub(crate) fn dispatch_authorization_resolution_id(
    intent_id: &str,
    selected_run_seq: Option<u64>,
    superseded_run_seqs: &[u64],
) -> String {
    let superseded = superseded_run_seqs
        .iter()
        .map(u64::to_string)
        .collect::<Vec<_>>()
        .join(",");
    let input = format!(
        "dispatch-authorization-resolution-v1\0{intent_id}\0{}\0{superseded}",
        sequence_label(selected_run_seq)
    );
    format!(
        "dispatch-authorization:{:x}",
        Sha256::digest(input.as_bytes())
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::agent_control::{AgentType, InvocationStatus, RecoveryAuthorization};
    use serde_json::json;

    const INTENT: &str =
        "blocked-resume:31a609e66888ed901ed1fb857e9a1bd5f0a0373bbb0a9ed600cb1ffba5bb187b";
    const SLICE: &str = "issue-814-substitution-model-kani";
    const CALLER: &str = "substitution-model-kani-stage";
    const OLD_CONFIG: &str =
        "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4";
    const NEW_CONFIG: &str =
        "sha256:v2:b4c93f8d86d2c164602b2275291b0f89dbefb4806579d8d8fc0074f2354f22d7";
    const OLD_ROUTE: &str =
        "sha256:v1:13236139e5e0a5aac217d2dac60e0b69c1dbc953a356618a97c8c1367f467724";
    const NEW_ROUTE: &str =
        "sha256:v1:d51c3ec358347f696336eee12a874221377a354c95cfec21a0ec8066c4c4e3b3";
    const POLICY: &str =
        "sha256:v1:9e36b5ee5a50d2c833aaeac004b994a441ec3f1c140639f3f257203069bebde2";
    const MANIFEST: &str = "4be5ddad961dc30507635801b5b7c6e0956e9a78f2f67ef39e0345abf0e5d407";

    fn declaration(
        run_seq: u64,
        intent: &str,
        caller: &str,
        scope: &str,
        configuration: &str,
        route: &str,
        started_at: f64,
    ) -> Value {
        json!({
            "intent_id": intent,
            "boundary": "dispatch_intended",
            "slice_id": scope,
            "role": "worker",
            "harness": "opencode/opencode-go/deepseek-v4.1-flash",
            "agent_type": "opencode",
            "model": "opencode-go/deepseek-v4.1-flash",
            "effort": "high",
            "route_configuration_fingerprint": configuration,
            "route_policy_fingerprint": POLICY,
            "route_manifest_digest": MANIFEST,
            "route_fingerprint": route,
            "started_at": started_at,
            "attempt": 3,
            "dispatch_generation": 3,
            "controller_epoch": "98d3fa2d86b97841a3f571fed96251b2",
            "agent_id_ignored": caller,
            "run_seq": run_seq,
        })
    }

    fn expected<'a>() -> ExpectedDispatchAuthorization<'a> {
        ExpectedDispatchAuthorization {
            intent_id: INTENT,
            caller: CALLER,
            scope_id: SLICE,
            role: "worker",
            harness: "opencode/opencode-go/deepseek-v4.1-flash",
            agent_type: "opencode",
            model: Some("opencode-go/deepseek-v4.1-flash"),
            effort: Some("high"),
            configuration_fingerprint: NEW_CONFIG,
            policy_fingerprint: Some(POLICY),
            manifest_digest: Some(MANIFEST),
            route_fingerprint: NEW_ROUTE,
        }
    }

    fn row<'a>(data: &'a Value, caller: &'a str, run_seq: Option<u64>) -> DispatchDeclaration<'a> {
        DispatchDeclaration {
            run_seq,
            agent_id: Some(caller),
            data,
        }
    }

    fn predecessor() -> InvocationRecord {
        let mut invocation = invocation("f285efc5-4b0d-4a42-bcf8-c50a329f4a1d", 2);
        invocation.status = InvocationStatus::Failed;
        invocation.ended_at = Some(20);
        invocation.exit_code = Some(1);
        invocation
    }

    fn invocation(invocation_id: &str, generation: u64) -> InvocationRecord {
        InvocationRecord {
            invocation_id: invocation_id.to_string(),
            runtime: AgentType::OpenCode,
            trigger: InvocationTrigger::Spawn,
            mode: Default::default(),
            routing: crate::domain::RoutingInfo::window(
                crate::services::tmux_ipc::WindowId::parse("@42").unwrap(),
            ),
            started_at: 10,
            ended_at: None,
            status: InvocationStatus::Running,
            exit_code: None,
            pr_number: None,
            head_sha: None,
            model: Some("opencode-go/deepseek-v4.1-flash".to_string()),
            effort: Some("high".to_string()),
            prior_invocation_id: None,
            recovery_round: 0,
            authorization_source: Some(RecoveryAuthorization::HumanApproved),
            generation,
            controller_epoch: Some("98d3fa2d86b97841a3f571fed96251b2".to_string()),
            runtime_agent_id: Some(SLICE.to_string()),
            slice_id: Some(SLICE.to_string()),
            branch: Some(format!("main.kani.{SLICE}")),
            worktree: Some(format!(".exo/worktrees/{SLICE}")),
            exit_reason: None,
            exit_classification: None,
            stderr_tail: None,
            role_route: None,
            usage: None,
            build: String::new(),
            launch_command: None,
            launch_command_source: None,
            config_sources: None,
        }
    }

    fn linked_successor(predecessor_id: &str) -> InvocationRecord {
        let mut successor = invocation("generation-3", 3);
        successor.trigger = InvocationTrigger::ResumeBlocked;
        successor.prior_invocation_id = Some(predecessor_id.to_string());
        successor.role_route = Some(RoleRouteProvenance {
            role: "worker".to_string(),
            harness: "opencode/opencode-go/deepseek-v4.1-flash".to_string(),
            agent_type: AgentType::OpenCode,
            model: Some("opencode-go/deepseek-v4.1-flash".to_string()),
            effort: Some("high".to_string()),
            configuration_fingerprint: NEW_CONFIG.to_string(),
            policy_fingerprint: Some(POLICY.to_string()),
            manifest_digest: Some(MANIFEST.to_string()),
            route_fingerprint: NEW_ROUTE.to_string(),
            dispatch_intent_id: INTENT.to_string(),
            scope_id: SLICE.to_string(),
        });
        successor
    }

    fn old_and_new<'a>(
        old: &'a Value,
        new: &'a Value,
    ) -> (DispatchDeclaration<'a>, DispatchDeclaration<'a>) {
        (
            row(old, CALLER, Some(188_678)),
            row(new, CALLER, Some(188_878)),
        )
    }

    #[test]
    fn missing_declaration_is_refused_with_the_exact_message() {
        let error = resolve_dispatch_declaration(&[], &[], expected(), None)
            .err()
            .expect("missing declaration must be refused")
            .to_string();
        assert_eq!(
            error,
            "Invalid input: role route dispatch authorization is missing or ambiguous"
        );
    }

    #[test]
    fn reauthorized_resume_resolves_the_current_declaration_over_a_proven_no_effect_predecessor() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &owner,
        };
        let resolution =
            resolve_dispatch_declaration(&[old_row, new_row], &[], expected(), Some(&proof))
                .expect("the reauthorized declaration is the only live authorization");
        assert_eq!(resolution.selected_run_seq, Some(188_878));
        assert_eq!(resolution.superseded_run_seqs, vec![188_678]);
    }

    #[test]
    fn ambiguity_without_no_effect_evidence_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let (old_row, new_row) = old_and_new(&old, &new);
        let error = resolve_dispatch_declaration(&[old_row, new_row], &[], expected(), None)
            .err()
            .expect("unproven supersession must be refused")
            .to_string();
        assert!(error.contains("verified no-effect evidence"), "{error}");
    }

    #[test]
    fn equivalent_duplicate_declarations_collapse_to_one_observation() {
        let first = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let second = declaration(188_879, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let resolution = resolve_dispatch_declaration(
            &[
                row(&first, CALLER, Some(188_878)),
                row(&second, CALLER, Some(188_879)),
            ],
            &[],
            expected(),
            None,
        )
        .expect("identical repeated observations are idempotent");
        assert_eq!(resolution.selected_run_seq, Some(188_878));
        assert!(resolution.superseded_run_seqs.is_empty());
    }

    #[test]
    fn competing_declarations_with_the_same_authorization_are_refused() {
        let first = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let second = declaration(188_879, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 9.0);
        let error = resolve_dispatch_declaration(
            &[
                row(&first, CALLER, Some(188_878)),
                row(&second, CALLER, Some(188_879)),
            ],
            &[],
            expected(),
            None,
        )
        .err()
        .expect("distinct declarations under one authorization must not collapse")
        .to_string();
        assert!(error.contains("competing declarations"), "{error}");
        assert!(error.contains("188878, 188879"), "{error}");
    }

    #[test]
    fn a_later_distinct_declaration_is_conflicting() {
        let old = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let later = declaration(188_900, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 3.0);
        let owner = predecessor();
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &owner,
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_878)),
                row(&later, CALLER, Some(188_900)),
            ],
            &[],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a later competing declaration must be refused")
        .to_string();
        assert!(
            error.contains("compete with the selected declaration"),
            "{error}"
        );
    }

    #[test]
    fn wrong_caller_is_not_a_match() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let owner = predecessor();
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &owner,
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, "foreign-stage", Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a declaration attributed to another caller is conflicting evidence")
        .to_string();
        assert!(
            error.contains("not attributed to the requesting caller"),
            "{error}"
        );
    }

    #[test]
    fn wrong_scope_is_not_a_match() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(
            188_878,
            INTENT,
            CALLER,
            "other-slice",
            NEW_CONFIG,
            NEW_ROUTE,
            2.0,
        );
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            expected(),
            None,
        )
        .err()
        .expect("a scope mismatch must not match")
        .to_string();
        assert!(
            error.contains("does not match any durable declaration"),
            "{error}"
        );
    }

    #[test]
    fn stale_configuration_declaration_is_not_a_match() {
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            ExpectedDispatchAuthorization {
                configuration_fingerprint: OLD_CONFIG,
                route_fingerprint: OLD_ROUTE,
                ..expected()
            },
            Some(&DispatchNoEffectProof {
                expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
                invocation: &predecessor(),
            }),
        )
        .err()
        .expect("a stale accepted configuration cannot authorize the old declaration")
        .to_string();
        assert!(
            error.contains("compete with the selected declaration"),
            "{error}"
        );
    }

    #[test]
    fn forged_route_fingerprint_is_not_a_match() {
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let error = resolve_dispatch_declaration(
            &[row(&new, CALLER, Some(188_878))],
            &[],
            ExpectedDispatchAuthorization {
                route_fingerprint: "sha256:v1:forged",
                ..expected()
            },
            None,
        )
        .err()
        .expect("a forged route fingerprint must not match")
        .to_string();
        assert!(
            error.contains("does not match any durable declaration"),
            "{error}"
        );
    }

    #[test]
    fn prior_launch_row_blocks_supersession() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let owner = predecessor();
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &owner,
        };
        let launch = json!({"intent_id": INTENT, "invocation_id": "someone-else"});
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[row(&launch, CALLER, Some(188_700))],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("an existing launch row must block a second launch")
        .to_string();
        assert!(
            error.contains("has a launch row at run_seq 188700"),
            "{error}"
        );
    }

    #[test]
    fn live_predecessor_blocks_supersession() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let owner = invocation("f285efc5-4b0d-4a42-bcf8-c50a329f4a1d", 2);
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &owner,
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a live predecessor means the intent may still be in flight")
        .to_string();
        assert!(error.contains("still live"), "{error}");
    }

    #[test]
    fn unknown_owner_invocation_outcome_blocks_supersession() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let mut owner = invocation("some-other-invocation", 2);
        owner.status = InvocationStatus::Failed;
        owner.prior_invocation_id = Some("different-predecessor".to_string());
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &owner,
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("an unresolved owner invocation must stay held")
        .to_string();
        assert!(error.contains("outcome is unresolved"), "{error}");
    }

    #[test]
    fn linked_successor_after_response_loss_resolves_adoption() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let successor = linked_successor("f285efc5-4b0d-4a42-bcf8-c50a329f4a1d");
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &successor,
        };
        let launch = json!({"intent_id": INTENT, "invocation_id": "generation-3"});
        let resolution = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[row(&launch, CALLER, Some(188_900))],
            expected(),
            Some(&proof),
        )
        .expect("a single linked successor proves the older declaration had no launch");
        assert_eq!(resolution.selected_run_seq, Some(188_878));
        assert_eq!(resolution.superseded_run_seqs, vec![188_678]);
    }

    #[test]
    fn a_second_launch_row_blocks_adoption() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let successor = linked_successor("f285efc5-4b0d-4a42-bcf8-c50a329f4a1d");
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &successor,
        };
        let first = json!({"intent_id": INTENT, "invocation_id": "generation-3"});
        let second = json!({"intent_id": INTENT, "invocation_id": "generation-4"});
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[
                row(&first, CALLER, Some(188_900)),
                row(&second, CALLER, Some(188_901)),
            ],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("two launch rows mean the intent launched more than once")
        .to_string();
        assert!(error.contains("2 launch rows"), "{error}");
    }

    #[test]
    fn unsequenced_declarations_are_refused() {
        let old = declaration(0, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(0, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let owner = predecessor();
        let proof = DispatchNoEffectProof {
            expected_invocation_id: "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d",
            invocation: &owner,
        };
        let error = resolve_dispatch_declaration(
            &[row(&old, CALLER, None), row(&new, CALLER, None)],
            &[],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("declarations without ledger sequence cannot be ordered")
        .to_string();
        assert!(error.contains("no durable ledger sequence"), "{error}");
    }

    #[test]
    fn resolution_identity_is_stable_and_sequence_specific() {
        let first = dispatch_authorization_resolution_id(INTENT, Some(188_878), &[188_678]);
        assert_eq!(
            first,
            dispatch_authorization_resolution_id(INTENT, Some(188_878), &[188_678])
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(INTENT, Some(188_879), &[188_678])
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(INTENT, Some(188_878), &[188_679])
        );
    }
}

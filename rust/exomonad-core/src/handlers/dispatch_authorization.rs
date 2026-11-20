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
//! the same intent id must be bound to durable evidence of the exact permitted
//! prelaunch configuration refusal: a host-recorded `tool.called` row for
//! `resume_blocked_leaf` that carries the superseded declaration's full route
//! tuple, the exact configuration-not-accepted error envelope, and no result.
//! The refusal must appear after that declaration and before the next distinct
//! observation. Declarations are grouped by route tuple *and* observation
//! fields (`started_at`, `attempt`, `dispatch_generation`, `controller_epoch`),
//! so a single refusal after two distinct observations cannot release both.
//!
//! The caller-supplied `human_approved` boolean is not durable approval either.
//! The host reads the authoritative controller checkpoint: exactly one of this
//! slice's recovery gates must still be `approved`, owned by the same run and
//! controller epoch that emitted the selected declaration. Pending, rejected,
//! additional or stale gates hold the supersession. Ledger `tl.gate_answered`
//! rows are corroboration only, because the controller persists the checkpoint
//! before its best-effort emission; the real #1511 approval exists only in the
//! checkpoint and must not require a fabricated historical event.
//!
//! The refusal proof is combined with owner evidence: either the owner
//! invocation is still the exact predecessor named by the request and no launch
//! row exists for the intent, or the owner invocation is the single linked
//! successor of that predecessor, its stored role route is this authorization,
//! and exactly one launch row names that successor. The proof is derived from
//! the ledger and the owner registry, never from the caller's claim about why an
//! earlier attempt failed. A missing launch row, a generic failed request, a
//! non-live predecessor, or an unrelated refusal is never sufficient.

use crate::effects::{EffectError, EffectResult};
use crate::services::agent_control::{InvocationRecord, InvocationTrigger, RoleRouteProvenance};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

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
    pub superseded_refusal_run_seqs: Vec<u64>,
    pub supersession_approval: Option<DispatchApprovalIdentity>,
}

/// Durable identity of the current operator approval that released a supersession.
///
/// The checkpoint gate is authoritative for "current"; the ledger answer is
/// optional corroboration because the controller persists the gate decision
/// before its best-effort `tl.gate_answered` emission. The historical #1511
/// approval exists only in the checkpoint, so requiring a fabricated ledger row
/// would reject the real durable decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct DispatchApprovalIdentity {
    pub gate_name: String,
    pub run_id: String,
    pub controller_epoch: String,
    pub source: &'static str,
    pub ledger_answer_run_seq: Option<u64>,
}

/// The authoritative current recovery-gate state read from the controller checkpoint.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct DispatchCurrentGate {
    pub run_id: String,
    pub controller_epoch: String,
    pub scope_id: String,
    pub gate_name: String,
    pub status: String,
    /// Any other recovery gate for the same slice present in the checkpoint.
    pub other_recovery_gates: Vec<(String, String)>,
}

/// One durable `tl.gate_answered` row for the leaf's recovery boundary.
///
/// The host cannot trust the request's caller-supplied `human_approved` flag as
/// proof that an operator approved this supersession. The controller records
/// every gate decision in the authoritative ledger, so the exact per-slice
/// recovery gate and its latest decision are the durable approval proof.
pub(crate) struct DispatchGateAnswer<'a> {
    pub run_seq: Option<u64>,
    pub data: &'a Value,
}

/// One host-recorded `tool.called` row for `resume_blocked_leaf`.
///
/// The row is written by the standalone server after the handler returns, so it
/// is durable evidence that the named authorization was refused before any
/// launch. Callers cannot manufacture this row through the request.
pub(crate) struct DispatchPrelaunchRefusal<'a> {
    pub run_seq: Option<u64>,
    pub caller: Option<&'a str>,
    pub data: &'a Value,
}

/// The exact permitted prelaunch refusal emitted before a launch.
pub(crate) const ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED: &str =
    "Invalid input: role route configuration is stale or was not accepted by this server";

/// Durable owner evidence that bounds which declaration could have launched.
pub(crate) struct DispatchNoEffectProof<'a> {
    pub expected_invocation_id: &'a str,
    pub expected_issue_id: u64,
    pub expected_branch: &'a str,
    pub expected_worktree_fingerprint: &'a str,
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
///
/// `refusals` are host-recorded `tool.called` rows for `resume_blocked_leaf`.
/// Every superseded declaration must be bound to its own exact permitted
/// prelaunch refusal; absence, an unrelated refusal, contradictory evidence or a
/// wrong-declaration refusal holds the retry before launch.
pub(crate) fn resolve_dispatch_declaration(
    declarations: &[DispatchDeclaration<'_>],
    launch_rows: &[DispatchDeclaration<'_>],
    refusals: &[DispatchPrelaunchRefusal<'_>],
    gate_answers: &[DispatchGateAnswer<'_>],
    current_gate: Option<&DispatchCurrentGate>,
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
    verify_refusal_ledger(declarations, refusals, &expected)?;
    if others.is_empty() {
        return Ok(DispatchAuthorizationResolution {
            selected_run_seq: selected.run_seq,
            superseded_run_seqs: Vec::new(),
            superseded_refusal_run_seqs: Vec::new(),
            supersession_approval: None,
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
    let approval = verify_supersession_approval(gate_answers, current_gate, selected, &expected)?;
    let groups = group_historical_observations(&others, selected_seq)?;
    for declaration in others.iter().copied() {
        verify_no_launch_effect(declaration, launch_state, proof, launch_rows, &expected)?;
    }
    verify_refusal_windows(&groups, selected_seq, refusals, &expected)?;
    let mut superseded = Vec::new();
    let mut superseded_refusals = Vec::new();
    for (index, group) in groups.iter().enumerate() {
        for declaration in group.iter().copied() {
            superseded.push(declaration.run_seq.ok_or_else(|| {
                EffectError::invalid_input(
                    "a historical dispatch declaration has no durable ledger sequence",
                )
            })?);
        }
        let window_end = groups
            .get(index + 1)
            .and_then(|next| next.first())
            .and_then(|declaration| declaration.run_seq)
            .unwrap_or(selected_seq);
        superseded_refusals.extend(verify_group_prelaunch_refusal(
            group, window_end, refusals, proof, &expected,
        )?);
    }
    superseded.sort_unstable();
    superseded_refusals.sort_unstable();
    Ok(DispatchAuthorizationResolution {
        selected_run_seq: selected.run_seq,
        superseded_run_seqs: superseded,
        superseded_refusal_run_seqs: superseded_refusals,
        supersession_approval: Some(approval),
    })
}

/// Group historical declarations into distinct observations.
///
/// `ObservedIdentity` deliberately omits the observation fields (start time,
/// attempt, dispatch generation and controller epoch), because the current
/// request does not carry them. Historical declarations must not be grouped by
/// that route tuple alone: two attempts that share a route but differ in any of
/// those fields are distinct declarations, and one refusal cannot prove both.
/// Equivalent re-observations of the same declaration stay in one group.
fn group_historical_observations<'a>(
    others: &[&'a DispatchDeclaration<'a>],
    selected_seq: u64,
) -> EffectResult<Vec<Vec<&'a DispatchDeclaration<'a>>>> {
    let mut groups: Vec<Vec<&DispatchDeclaration>> = Vec::new();
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
        if let Some(group) = groups.iter_mut().find(|group| {
            same_observation(group[0], declaration)
                && ObservedIdentity::read(group[0]) == ObservedIdentity::read(declaration)
        }) {
            group.push(declaration);
        } else {
            groups.push(vec![declaration]);
        }
    }
    for group in &mut groups {
        group.sort_by_key(|declaration| declaration.run_seq.unwrap_or(u64::MAX));
    }
    groups.sort_by_key(|group| group[0].run_seq.unwrap_or(u64::MAX));
    Ok(groups)
}

/// Reject refusals that cannot belong to any historical observation window.
fn verify_refusal_windows(
    groups: &[Vec<&DispatchDeclaration<'_>>],
    selected_seq: u64,
    refusals: &[DispatchPrelaunchRefusal<'_>],
    expected: &ExpectedDispatchAuthorization<'_>,
) -> EffectResult<()> {
    let Some(first) = groups.first() else {
        return Ok(());
    };
    let first_observation = ObservedIdentity::read(first[0]);
    let first_sequence = first
        .last()
        .and_then(|declaration| declaration.run_seq)
        .ok_or_else(|| {
            EffectError::invalid_input(
                "a historical dispatch declaration has no durable ledger sequence",
            )
        })?;
    for refusal in refusals {
        let Some(identity) = refusal_identity(refusal) else {
            continue;
        };
        if identity.intent_id != expected.intent_id
            || !groups
                .iter()
                .any(|group| ObservedIdentity::read(group[0]) == identity)
        {
            continue;
        }
        let sequence = refusal.run_seq.ok_or_else(|| {
            EffectError::invalid_input("a durable prelaunch refusal has no durable ledger sequence")
        })?;
        if sequence <= first_sequence {
            return Err(EffectError::invalid_input(format!(
                "durable prelaunch refusal at run_seq {sequence} does not follow the \
                 superseded declaration at run_seq {first_sequence} for intent {}",
                first_observation.intent_id
            )));
        }
        if sequence >= selected_seq {
            return Err(EffectError::invalid_input(format!(
                "durable prelaunch refusal at run_seq {sequence} is not a superseded refusal \
                 before the selected declaration at run_seq {selected_seq}"
            )));
        }
    }
    Ok(())
}

/// Require one exact durable refusal inside each distinct observation's window.
///
/// A window runs from the last declaration of an observation group to the first
/// declaration of the next group (or the selected declaration). Because the
/// windows are disjoint, a single refusal can only ever be bound to one group;
/// a refusal positioned after two distinct observations cannot release both.
fn verify_group_prelaunch_refusal(
    group: &[&DispatchDeclaration<'_>],
    window_end: u64,
    refusals: &[DispatchPrelaunchRefusal<'_>],
    proof: &DispatchNoEffectProof<'_>,
    expected: &ExpectedDispatchAuthorization<'_>,
) -> EffectResult<Vec<u64>> {
    let wanted = ObservedIdentity::read(group[0]);
    let start = group
        .last()
        .and_then(|declaration| declaration.run_seq)
        .ok_or_else(|| {
            EffectError::invalid_input(
                "a historical dispatch declaration has no durable ledger sequence",
            )
        })?;
    let mut accepted = Vec::new();
    for refusal in refusals {
        let Some(identity) = refusal_identity(refusal) else {
            continue;
        };
        if identity != wanted {
            continue;
        }
        let sequence = refusal.run_seq.ok_or_else(|| {
            EffectError::invalid_input("a durable prelaunch refusal has no durable ledger sequence")
        })?;
        if sequence <= start || sequence >= window_end {
            continue;
        }
        verify_refusal_envelope(refusal, proof, expected)?;
        accepted.push(sequence);
    }
    if accepted.is_empty() {
        return Err(EffectError::invalid_input(format!(
            "superseded dispatch declaration at run_seq {} has no durable verified prelaunch \
             configuration refusal bound to that exact observation",
            sequence_list(group.iter().map(|declaration| declaration.run_seq))
        )));
    }
    Ok(accepted)
}

/// Require a durable, current, provenance-bound operator approval.
///
/// `human_approved` in the request is caller-supplied and cannot prove anything.
/// The controller checkpoint is authoritative for the *current* gate state: it
/// persists the decision before emitting the best-effort `tl.gate_answered`
/// event, so an event-only read can accept a stale approval after a later
/// rejection or reopen whose emission failed, or accept one gate's approval
/// while another same-slice recovery gate is still pending.
///
/// The checkpoint must belong to the run that emitted the selected declaration
/// (`agent_id` + `controller_epoch`) and must still own the declared scope. Its
/// sole same-slice recovery gate must be `approved`; any pending, rejected or
/// additional recovery gate holds the supersession. Ledger answers are
/// corroboration only and must not contradict the checkpoint. This deliberately
/// accepts the real historical #1511 shape: an approved checkpoint with no
/// matching `tl.gate_answered` row and a selected declaration that predates any
/// new event.
fn verify_supersession_approval(
    gate_answers: &[DispatchGateAnswer<'_>],
    current_gate: Option<&DispatchCurrentGate>,
    selected: &DispatchDeclaration<'_>,
    expected: &ExpectedDispatchAuthorization<'_>,
) -> EffectResult<DispatchApprovalIdentity> {
    let gate = current_gate.ok_or_else(|| {
        EffectError::invalid_input(format!(
            "supersession of historical dispatch declarations requires a durable approved \
             operator decision from the current recovery gate of scope {:?}",
            expected.scope_id
        ))
    })?;
    let gate_names = slice_recovery_gate_names(expected.scope_id);
    if gate.scope_id != expected.scope_id || !gate_names.iter().any(|name| name == &gate.gate_name)
    {
        return Err(EffectError::invalid_input(format!(
            "durable recovery gate {:?} is not a recovery gate of the declared scope {:?}",
            gate.gate_name, expected.scope_id
        )));
    }
    if gate.run_id.trim().is_empty() || gate.controller_epoch.trim().is_empty() {
        return Err(EffectError::invalid_input(
            "the durable current recovery gate has no run or controller provenance",
        ));
    }
    if selected.agent_id != Some(gate.run_id.as_str()) {
        return Err(EffectError::invalid_input(format!(
            "the selected dispatch declaration is not attributed to the run that owns the \
             current recovery gate (declaration {:?}, gate run {:?})",
            selected.agent_id, gate.run_id
        )));
    }
    let declaration_epoch = selected
        .data
        .get("controller_epoch")
        .and_then(Value::as_str)
        .filter(|epoch| !epoch.is_empty())
        .ok_or_else(|| {
            EffectError::invalid_input(
                "the selected dispatch declaration has no durable controller epoch",
            )
        })?;
    if declaration_epoch != gate.controller_epoch {
        return Err(EffectError::invalid_input(format!(
            "the current recovery gate controller epoch {:?} does not match the selected \
             declaration controller epoch {:?}",
            gate.controller_epoch, declaration_epoch
        )));
    }
    match gate.status.as_str() {
        "approved" => {}
        "pending" => {
            return Err(EffectError::invalid_input(format!(
                "the current durable recovery gate {:?} is pending; an approved current \
                 supersession is required",
                gate.gate_name
            )));
        }
        "rejected" => {
            return Err(EffectError::invalid_input(format!(
                "the current durable recovery gate decision is {:?}; an approved current \
                 supersession is required",
                gate.status
            )));
        }
        other => {
            return Err(EffectError::invalid_input(format!(
                "the current durable recovery gate {:?} has unknown status {other:?}; an \
                 approved current supersession is required",
                gate.gate_name
            )));
        }
    }
    for (name, status) in &gate.other_recovery_gates {
        match status.as_str() {
            "pending" => {
                return Err(EffectError::invalid_input(format!(
                    "another same-slice recovery gate {:?} is pending; an approved current \
                     supersession is required",
                    name
                )));
            }
            "rejected" => {
                return Err(EffectError::invalid_input(format!(
                    "another same-slice recovery gate {:?} was rejected; an approved current \
                     supersession is required",
                    name
                )));
            }
            "approved" => {
                return Err(EffectError::invalid_input(format!(
                    "multiple same-slice recovery gates are approved ({:?} and {:?}); the \
                     current approval is ambiguous",
                    gate.gate_name, name
                )));
            }
            other => {
                return Err(EffectError::invalid_input(format!(
                    "another same-slice recovery gate {:?} has unknown status {other:?}; the \
                     current approval is ambiguous",
                    name
                )));
            }
        }
    }

    let mut decisions = Vec::new();
    for answer in gate_answers {
        let Some(name) = answer.data.get("gate_name").and_then(Value::as_str) else {
            continue;
        };
        if !gate_names.iter().any(|candidate| candidate == name) {
            continue;
        }
        let sequence = answer.run_seq.ok_or_else(|| {
            EffectError::invalid_input("a durable gate answer has no durable ledger sequence")
        })?;
        let decision = answer
            .data
            .get("decision")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                EffectError::invalid_input(format!(
                    "durable gate answer at run_seq {sequence} has no decision"
                ))
            })?;
        decisions.push((sequence, name, decision));
    }
    decisions.sort_by_key(|(sequence, _, _)| *sequence);
    let ledger_answer_run_seq = if let Some((sequence, name, decision)) = decisions.last().copied()
    {
        if name != gate.gate_name {
            return Err(EffectError::invalid_input(format!(
                "the latest durable recovery gate answer at run_seq {sequence} belongs to {:?}, \
                 not the current gate {:?}",
                name, gate.gate_name
            )));
        }
        if decision != "approved" {
            return Err(EffectError::invalid_input(format!(
                "the latest durable recovery gate decision at run_seq {sequence} is {decision:?}, \
                 which conflicts with the current durable checkpoint approval; an approved \
                 current supersession is required"
            )));
        }
        Some(sequence)
    } else {
        None
    };
    // A checkpoint approval is current regardless of when the declaration was
    // written. The real historical #1511 declaration predates any later event,
    // so ledger ordering is not the authority; the run/epoch-bound checkpoint
    // is. A corroborating answer is accepted whether it predates or follows the
    // selected declaration as long as it does not contradict the checkpoint.
    Ok(DispatchApprovalIdentity {
        gate_name: gate.gate_name.clone(),
        run_id: gate.run_id.clone(),
        controller_epoch: gate.controller_epoch.clone(),
        source: if ledger_answer_run_seq.is_some() {
            "checkpoint_and_ledger"
        } else {
            "checkpoint"
        },
        ledger_answer_run_seq,
    })
}

fn slice_recovery_gate_names(scope_id: &str) -> [String; 3] {
    [
        format!("tl-terminal-owner-recovery-{scope_id}"),
        format!("tl-dispatch-failed-{scope_id}"),
        format!("tl-dispatch-ownership-conflict-{scope_id}"),
    ]
}

/// Read the authoritative current recovery-gate state for one slice.
///
/// The controller checkpoint is the only place a gate decision is guaranteed to
/// be durable: `_set_gate` writes the checkpoint before its best-effort
/// `tl.gate_answered` emission. Historical #1511 proves the gap is real - the
/// checkpoint records `approved` while the ledger has no matching event.
///
/// The scan is bounded and refuses symlinks. A checkpoint is a candidate only
/// when it names one of this slice's recovery gates, and it must also declare
/// the slice itself. Exactly one candidate checkpoint may exist; otherwise the
/// current state is ambiguous and the caller must fail closed.
pub(crate) fn read_current_recovery_gate(
    project_dir: &Path,
    scope_id: &str,
) -> EffectResult<DispatchCurrentGate> {
    let loop_root = project_dir.join(".exo/tl-loop");
    if !loop_root.is_dir() {
        return Err(EffectError::invalid_input(format!(
            "no durable controller checkpoint is available for the recovery gate of scope \
             {scope_id:?}"
        )));
    }
    let gate_names = slice_recovery_gate_names(scope_id);
    let mut candidates = Vec::new();
    let mut pending = vec![(loop_root, 0usize)];
    let mut visited = 0usize;
    while let Some((directory, depth)) = pending.pop() {
        if depth > 32 || visited > 20_000 {
            return Err(EffectError::invalid_input(
                "controller recovery-gate scan exceeded its safety bound",
            ));
        }
        visited += 1;
        let checkpoint = directory.join("run.json");
        if let Some(candidate) = read_recovery_gate_checkpoint(&checkpoint, &gate_names, scope_id)?
        {
            candidates.push(candidate);
        }
        let entries = fs::read_dir(&directory).map_err(|error| {
            EffectError::invalid_input(format!(
                "cannot inspect controller state {}: {error}",
                directory.display()
            ))
        })?;
        for entry in entries {
            let entry = entry.map_err(|error| {
                EffectError::invalid_input(format!(
                    "cannot inspect controller state {}: {error}",
                    directory.display()
                ))
            })?;
            let file_type = entry.file_type().map_err(|error| {
                EffectError::invalid_input(format!(
                    "cannot inspect controller state {}: {error}",
                    entry.path().display()
                ))
            })?;
            if file_type.is_dir() {
                pending.push((entry.path(), depth + 1));
            }
        }
    }
    let [candidate] = candidates.as_slice() else {
        return Err(EffectError::invalid_input(format!(
            "expected exactly one durable controller checkpoint naming a recovery gate for scope \
             {scope_id:?}, found {}",
            candidates.len()
        )));
    };
    let matching = candidate
        .gates
        .iter()
        .filter(|(name, _)| gate_names.iter().any(|gate| gate == name))
        .collect::<Vec<_>>();
    let primary = matching
        .iter()
        .find(|(_, status)| status == "approved")
        .copied()
        .or_else(|| matching.first().copied())
        .ok_or_else(|| {
            EffectError::invalid_input(format!(
                "durable controller checkpoint {} names no recovery gate for scope {scope_id:?}",
                candidate.path.display()
            ))
        })?;
    let others = matching
        .iter()
        .filter(|(name, _)| name != &primary.0)
        .map(|(name, status)| (name.clone(), status.clone()))
        .collect();
    Ok(DispatchCurrentGate {
        run_id: candidate.run_id.clone(),
        controller_epoch: candidate.controller_epoch.clone(),
        scope_id: scope_id.to_string(),
        gate_name: primary.0.clone(),
        status: primary.1.clone(),
        other_recovery_gates: others,
    })
}

struct RecoveryGateCheckpoint {
    path: PathBuf,
    run_id: String,
    controller_epoch: String,
    gates: Vec<(String, String)>,
}

fn read_recovery_gate_checkpoint(
    path: &Path,
    gate_names: &[String; 3],
    scope_id: &str,
) -> EffectResult<Option<RecoveryGateCheckpoint>> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(EffectError::invalid_input(format!(
                "cannot inspect controller checkpoint {}: {error}",
                path.display()
            )));
        }
    };
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(EffectError::invalid_input(format!(
            "controller checkpoint {} is not a regular file",
            path.display()
        )));
    }
    let bytes = fs::read(path).map_err(|error| {
        EffectError::invalid_input(format!(
            "cannot read controller checkpoint {}: {error}",
            path.display()
        ))
    })?;
    let document: Value = match serde_json::from_slice(&bytes) {
        Ok(document) => document,
        Err(error) => {
            let mentions_gate = gate_names
                .iter()
                .any(|name| String::from_utf8_lossy(&bytes).contains(name.as_str()));
            if mentions_gate {
                return Err(EffectError::invalid_input(format!(
                    "controller checkpoint {} is malformed: {error}",
                    path.display()
                )));
            }
            return Ok(None);
        }
    };
    let gates = document
        .get("gates")
        .and_then(Value::as_array)
        .map(|gates| {
            gates
                .iter()
                .filter_map(|gate| {
                    Some((
                        gate.get("name").and_then(Value::as_str)?.to_string(),
                        gate.get("status").and_then(Value::as_str)?.to_string(),
                    ))
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    if !gates
        .iter()
        .any(|(name, _)| gate_names.iter().any(|gate| gate == name))
    {
        return Ok(None);
    }
    if !document
        .get("slices")
        .and_then(Value::as_object)
        .is_some_and(|slices| slices.get(scope_id).is_some_and(Value::is_object))
    {
        return Err(EffectError::invalid_input(format!(
            "controller checkpoint {} names a recovery gate but does not own scope {scope_id:?}",
            path.display()
        )));
    }
    let run_id = document
        .get("run_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            EffectError::invalid_input(format!(
                "controller checkpoint {} has no durable run identity",
                path.display()
            ))
        })?;
    let controller_epoch = document
        .get("controller_epoch")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            EffectError::invalid_input(format!(
                "controller checkpoint {} has no durable controller epoch",
                path.display()
            ))
        })?;
    Ok(Some(RecoveryGateCheckpoint {
        path: path.to_path_buf(),
        run_id: run_id.to_string(),
        controller_epoch: controller_epoch.to_string(),
        gates,
    }))
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

/// Whether two declarations are provably the same observation.
///
/// Every observation field must be present on both sides. A missing field is
/// not evidence of equality: two malformed historical declarations with the
/// same omissions would otherwise collapse into one observation and share one
/// refusal. Distinct/missing observations stay separate and each must prove its
/// own exact prelaunch refusal.
fn same_observation(left: &DispatchDeclaration<'_>, right: &DispatchDeclaration<'_>) -> bool {
    const FIELDS: [&str; 4] = [
        "started_at",
        "attempt",
        "dispatch_generation",
        "controller_epoch",
    ];
    FIELDS.iter().all(|field| {
        let left_value = left.data.get(*field);
        let right_value = right.data.get(*field);
        left_value.is_some() && left_value == right_value
    })
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

/// Reject a same-intent refusal row that no durable declaration can explain.
fn verify_refusal_ledger(
    declarations: &[DispatchDeclaration<'_>],
    refusals: &[DispatchPrelaunchRefusal<'_>],
    expected: &ExpectedDispatchAuthorization<'_>,
) -> EffectResult<()> {
    let declared = declarations
        .iter()
        .map(ObservedIdentity::read)
        .collect::<Vec<_>>();
    for refusal in refusals {
        let sequence = sequence_label(refusal.run_seq);
        let Some(arguments) = refusal.data.get("arguments") else {
            continue;
        };
        let top_level_intent = arguments.get("dispatch_intent_id").and_then(Value::as_str);
        let route_intent = arguments
            .get("role_route")
            .and_then(|route| route.get("dispatch_intent_id"))
            .and_then(Value::as_str);
        if top_level_intent != Some(expected.intent_id) && route_intent != Some(expected.intent_id)
        {
            continue;
        }
        if refusal.data.get("tool_name").and_then(Value::as_str) != Some("resume_blocked_leaf") {
            return Err(EffectError::invalid_input(format!(
                "durable refusal at run_seq {sequence} is not attributed to resume_blocked_leaf"
            )));
        }
        let Some(identity) = refusal_identity(refusal) else {
            return Err(EffectError::invalid_input(format!(
                "durable prelaunch refusal at run_seq {sequence} is malformed or missing its \
                 dispatch authorization"
            )));
        };
        if identity.intent_id != expected.intent_id {
            return Err(EffectError::invalid_input(format!(
                "durable prelaunch refusal at run_seq {sequence} names a different dispatch intent"
            )));
        }
        if !declared.contains(&identity) {
            return Err(EffectError::invalid_input(format!(
                "durable prelaunch refusal at run_seq {sequence} is not bound to any durable \
                 dispatch declaration for this intent"
            )));
        }
    }
    Ok(())
}

fn refusal_identity(refusal: &DispatchPrelaunchRefusal<'_>) -> Option<ObservedIdentity> {
    let arguments = refusal.data.get("arguments")?;
    let route = arguments.get("role_route")?;
    if !route.is_object() {
        return None;
    }
    let text = |key: &str| route.get(key).and_then(Value::as_str).map(str::to_string);
    Some(ObservedIdentity {
        intent_id: text("dispatch_intent_id").unwrap_or_default(),
        agent_id: refusal.caller.map(str::to_string),
        boundary: Some("dispatch_intended".to_string()),
        scope_id: text("scope_id"),
        role: text("role"),
        harness: text("harness"),
        agent_type: text("agent_type"),
        model: text("model"),
        effort: text("effort"),
        configuration_fingerprint: text("configuration_fingerprint"),
        policy_fingerprint: text("policy_fingerprint"),
        manifest_digest: text("manifest_digest"),
        route_fingerprint: text("route_fingerprint"),
    })
}

fn verify_refusal_envelope(
    refusal: &DispatchPrelaunchRefusal<'_>,
    proof: &DispatchNoEffectProof<'_>,
    expected: &ExpectedDispatchAuthorization<'_>,
) -> EffectResult<()> {
    let sequence = sequence_label(refusal.run_seq);
    let data = refusal.data;
    if refusal.caller != Some(expected.caller) {
        return Err(EffectError::invalid_input(format!(
            "durable prelaunch refusal at run_seq {sequence} is not attributed to the requesting \
             caller"
        )));
    }
    if data.get("tool_name").and_then(Value::as_str) != Some("resume_blocked_leaf")
        || data.get("success") != Some(&Value::Bool(false))
        || data.get("error").and_then(Value::as_str) != Some(ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED)
        || data.get("result").is_some_and(|result| !result.is_null())
    {
        return Err(EffectError::invalid_input(format!(
            "durable refusal at run_seq {sequence} is not the exact permitted prelaunch \
             configuration refusal"
        )));
    }
    let arguments = data.get("arguments").ok_or_else(|| {
        EffectError::invalid_input(format!(
            "durable prelaunch refusal at run_seq {sequence} has no request arguments"
        ))
    })?;
    if arguments.get("human_approved") != Some(&Value::Bool(true))
        || arguments.get("chainlink_issue_id").and_then(Value::as_u64)
            != Some(proof.expected_issue_id)
        || arguments
            .get("expected_invocation_id")
            .and_then(Value::as_str)
            != Some(proof.expected_invocation_id)
        || arguments.get("expected_branch").and_then(Value::as_str) != Some(proof.expected_branch)
        || arguments
            .get("expected_worktree_fingerprint")
            .and_then(Value::as_str)
            != Some(proof.expected_worktree_fingerprint)
    {
        return Err(EffectError::invalid_input(format!(
            "durable prelaunch refusal at run_seq {sequence} does not prove this exact owner \
             resume was refused"
        )));
    }
    Ok(())
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
    superseded_refusal_run_seqs: &[u64],
    approval: Option<&DispatchApprovalIdentity>,
) -> String {
    let superseded = superseded_run_seqs
        .iter()
        .map(u64::to_string)
        .collect::<Vec<_>>()
        .join(",");
    let refusals = superseded_refusal_run_seqs
        .iter()
        .map(u64::to_string)
        .collect::<Vec<_>>()
        .join(",");
    let approval = approval.map_or_else(
        || "none".to_string(),
        |approval| {
            format!(
                "{}\0{}\0{}\0{}\0{}",
                approval.gate_name,
                approval.run_id,
                approval.controller_epoch,
                approval.source,
                sequence_label(approval.ledger_answer_run_seq)
            )
        },
    );
    let input = format!(
        "dispatch-authorization-resolution-v4\0{intent_id}\0{}\0{superseded}\0{refusals}\0{approval}",
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
    const EPOCH: &str = "98d3fa2d86b97841a3f571fed96251b2";

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
            "controller_epoch": EPOCH,
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

    const PREDECESSOR: &str = "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d";
    const BRANCH: &str = "main.kani.issue-814-substitution-model-kani";
    const FINGERPRINT: &str =
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    fn old_and_new<'a>(
        old: &'a Value,
        new: &'a Value,
    ) -> (DispatchDeclaration<'a>, DispatchDeclaration<'a>) {
        (
            row(old, CALLER, Some(188_678)),
            row(new, CALLER, Some(188_878)),
        )
    }

    fn no_effect<'a>(invocation: &'a InvocationRecord) -> DispatchNoEffectProof<'a> {
        DispatchNoEffectProof {
            expected_invocation_id: PREDECESSOR,
            expected_issue_id: 814,
            expected_branch: BRANCH,
            expected_worktree_fingerprint: FINGERPRINT,
            invocation,
        }
    }

    fn refusal_data(declaration: &Value) -> Value {
        json!({
            "tool_name": "resume_blocked_leaf",
            "role": "tl",
            "arguments": {
                "chainlink_issue_id": 814,
                "expected_invocation_id": PREDECESSOR,
                "expected_branch": BRANCH,
                "expected_worktree_fingerprint": FINGERPRINT,
                "task": "resume the blocked leaf",
                "human_approved": true,
                "role_route": {
                    "dispatch_intent_id": declaration["intent_id"],
                    "scope_id": declaration["slice_id"],
                    "role": declaration["role"],
                    "harness": declaration["harness"],
                    "agent_type": declaration["agent_type"],
                    "model": declaration["model"],
                    "effort": declaration["effort"],
                    "configuration_fingerprint": declaration["route_configuration_fingerprint"],
                    "policy_fingerprint": declaration["route_policy_fingerprint"],
                    "manifest_digest": declaration["route_manifest_digest"],
                    "route_fingerprint": declaration["route_fingerprint"],
                },
            },
            "success": false,
            "error": ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED,
        })
    }

    fn refusal_row<'a>(
        data: &'a Value,
        run_seq: u64,
        caller: &'a str,
    ) -> DispatchPrelaunchRefusal<'a> {
        DispatchPrelaunchRefusal {
            run_seq: Some(run_seq),
            caller: Some(caller),
            data,
        }
    }

    fn approval_data(gate: &str, decision: &str) -> Value {
        json!({
            "gate_name": gate,
            "decision": decision,
            "source": "cli",
        })
    }

    fn approval_row<'a>(data: &'a Value, run_seq: u64) -> DispatchGateAnswer<'a> {
        DispatchGateAnswer {
            run_seq: Some(run_seq),
            data,
        }
    }

    fn approved_answers() -> Vec<DispatchGateAnswer<'static>> {
        let gate = format!("tl-terminal-owner-recovery-{SLICE}");
        let data = approval_data(&gate, "approved");
        vec![approval_row(Box::leak(Box::new(data)), 188_600)]
    }

    fn current_gate() -> DispatchCurrentGate {
        DispatchCurrentGate {
            run_id: CALLER.to_string(),
            controller_epoch: EPOCH.to_string(),
            scope_id: SLICE.to_string(),
            gate_name: format!("tl-terminal-owner-recovery-{SLICE}"),
            status: "approved".to_string(),
            other_recovery_gates: Vec::new(),
        }
    }

    /// Test wrapper: most cases exercise an approved current checkpoint gate.
    fn resolve<'a>(
        declarations: &[DispatchDeclaration<'a>],
        launch_rows: &[DispatchDeclaration<'a>],
        refusals: &[DispatchPrelaunchRefusal<'a>],
        gate_answers: &[DispatchGateAnswer<'a>],
        expected: ExpectedDispatchAuthorization<'a>,
        no_effect: Option<&DispatchNoEffectProof<'a>>,
    ) -> EffectResult<DispatchAuthorizationResolution> {
        let gate = current_gate();
        resolve_dispatch_declaration(
            declarations,
            launch_rows,
            refusals,
            gate_answers,
            Some(&gate),
            expected,
            no_effect,
        )
    }

    #[test]
    fn missing_declaration_is_refused_with_the_exact_message() {
        let error = resolve(&[], &[], &[], &approved_answers(), expected(), None)
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
        let old_refusal = refusal_data(&old);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let resolution = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .expect("the reauthorized declaration is the only live authorization");
        assert_eq!(resolution.selected_run_seq, Some(188_878));
        assert_eq!(resolution.superseded_run_seqs, vec![188_678]);
        assert_eq!(resolution.superseded_refusal_run_seqs, vec![188_693]);
        assert_eq!(
            resolution.supersession_approval,
            Some(DispatchApprovalIdentity {
                gate_name: format!("tl-terminal-owner-recovery-{SLICE}"),
                run_id: CALLER.to_string(),
                controller_epoch: EPOCH.to_string(),
                source: "checkpoint_and_ledger",
                ledger_answer_run_seq: Some(188_600),
            })
        );
    }

    #[test]
    fn ambiguity_without_no_effect_evidence_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let (old_row, new_row) = old_and_new(&old, &new);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            None,
        )
        .err()
        .expect("unproven supersession must be refused")
        .to_string();
        assert!(error.contains("verified no-effect evidence"), "{error}");
    }

    #[test]
    fn missing_prelaunch_refusal_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a non-live predecessor and an empty launch history are not refusal proof")
        .to_string();
        assert!(
            error.contains("has no durable verified prelaunch configuration refusal"),
            "{error}"
        );
        assert!(error.contains("188678"), "{error}");
    }

    #[test]
    fn unrelated_refusal_is_not_proof() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let unrelated = declaration(
            188_670,
            "blocked-resume:unrelated",
            CALLER,
            SLICE,
            OLD_CONFIG,
            OLD_ROUTE,
            1.0,
        );
        let unrelated_refusal = refusal_data(&unrelated);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&unrelated_refusal, 188_690, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("an unrelated refusal does not prove the superseded declaration was refused")
        .to_string();
        assert!(
            error.contains("has no durable verified prelaunch configuration refusal"),
            "{error}"
        );
    }

    #[test]
    fn refusal_for_a_wrong_declaration_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let wrong = declaration(188_680, INTENT, CALLER, SLICE, OLD_CONFIG, NEW_ROUTE, 1.0);
        let wrong_refusal = refusal_data(&wrong);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&wrong_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a refusal for another declaration must not release this one")
        .to_string();
        assert!(
            error.contains("not bound to any durable dispatch declaration")
                || error.contains("has no durable verified prelaunch configuration refusal"),
            "{error}"
        );
    }

    #[test]
    fn contradictory_refusal_envelope_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let mut old_refusal = refusal_data(&old);
        old_refusal["success"] = json!(true);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a successful request is not a refusal")
        .to_string();
        assert!(
            error.contains("not the exact permitted prelaunch configuration refusal"),
            "{error}"
        );
    }

    #[test]
    fn refusal_before_the_declaration_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&old_refusal, 188_670, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a refusal cannot predate the declaration it is said to refuse")
        .to_string();
        assert!(error.contains("does not follow"), "{error}");
    }

    #[test]
    fn refusal_after_the_selected_declaration_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&old_refusal, 188_900, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("the refusal must precede the selected supersession")
        .to_string();
        assert!(
            error.contains("not a superseded refusal before the selected declaration"),
            "{error}"
        );
    }

    #[test]
    fn refusal_attributed_to_another_caller_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let (old_row, new_row) = old_and_new(&old, &new);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[old_row, new_row],
            &[],
            &[refusal_row(&old_refusal, 188_693, "foreign-stage")],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a refusal recorded for another caller is not this resume's proof")
        .to_string();
        assert!(
            error.contains("has no durable verified prelaunch configuration refusal")
                || error.contains("not bound to any durable dispatch declaration"),
            "{error}"
        );
    }

    #[test]
    fn unrelated_same_intent_refusal_without_supersession_is_refused() {
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let orphan = declaration(188_680, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let orphan_refusal = refusal_data(&orphan);
        let error = resolve(
            &[row(&new, CALLER, Some(188_878))],
            &[],
            &[refusal_row(&orphan_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            None,
        )
        .err()
        .expect("a same-intent refusal for no durable declaration is contradictory history")
        .to_string();
        assert!(
            error.contains("not bound to any durable dispatch declaration"),
            "{error}"
        );
    }

    #[test]
    fn equivalent_duplicate_declarations_collapse_to_one_observation() {
        let first = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let second = declaration(188_879, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let resolution = resolve(
            &[
                row(&first, CALLER, Some(188_878)),
                row(&second, CALLER, Some(188_879)),
            ],
            &[],
            &[],
            &approved_answers(),
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
        let error = resolve(
            &[
                row(&first, CALLER, Some(188_878)),
                row(&second, CALLER, Some(188_879)),
            ],
            &[],
            &[],
            &approved_answers(),
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
        let proof = no_effect(&owner);
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_878)),
                row(&later, CALLER, Some(188_900)),
            ],
            &[],
            &[],
            &approved_answers(),
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
        let proof = no_effect(&owner);
        let error = resolve(
            &[
                row(&old, "foreign-stage", Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[],
            &approved_answers(),
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
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[],
            &approved_answers(),
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
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[],
            &approved_answers(),
            ExpectedDispatchAuthorization {
                configuration_fingerprint: OLD_CONFIG,
                route_fingerprint: OLD_ROUTE,
                ..expected()
            },
            Some(&no_effect(&predecessor())),
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
        let error = resolve(
            &[row(&new, CALLER, Some(188_878))],
            &[],
            &[],
            &approved_answers(),
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
    fn prior_launch_row_blocks_supersession_even_with_an_exact_refusal() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let launch = json!({"intent_id": INTENT, "invocation_id": "someone-else"});
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[row(&launch, CALLER, Some(188_700))],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
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
        let old_refusal = refusal_data(&old);
        let owner = invocation(PREDECESSOR, 2);
        let proof = no_effect(&owner);
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
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
        let old_refusal = refusal_data(&old);
        let mut owner = invocation("some-other-invocation", 2);
        owner.status = InvocationStatus::Failed;
        owner.prior_invocation_id = Some("different-predecessor".to_string());
        let proof = no_effect(&owner);
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
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
        let old_refusal = refusal_data(&old);
        let successor = linked_successor(PREDECESSOR);
        let proof = no_effect(&successor);
        let launch = json!({"intent_id": INTENT, "invocation_id": "generation-3"});
        let resolution = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[row(&launch, CALLER, Some(188_900))],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .expect("a single linked successor proves the older declaration had no launch");
        assert_eq!(resolution.selected_run_seq, Some(188_878));
        assert_eq!(resolution.superseded_run_seqs, vec![188_678]);
        assert_eq!(resolution.superseded_refusal_run_seqs, vec![188_693]);
    }

    #[test]
    fn adoption_without_the_exact_refusal_is_held() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let successor = linked_successor(PREDECESSOR);
        let proof = no_effect(&successor);
        let launch = json!({"intent_id": INTENT, "invocation_id": "generation-3"});
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[row(&launch, CALLER, Some(188_900))],
            &[],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("adoption cannot release an unproven historical declaration")
        .to_string();
        assert!(
            error.contains("has no durable verified prelaunch configuration refusal"),
            "{error}"
        );
    }

    #[test]
    fn a_second_launch_row_blocks_adoption() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let successor = linked_successor(PREDECESSOR);
        let proof = no_effect(&successor);
        let first = json!({"intent_id": INTENT, "invocation_id": "generation-3"});
        let second = json!({"intent_id": INTENT, "invocation_id": "generation-4"});
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[
                row(&first, CALLER, Some(188_900)),
                row(&second, CALLER, Some(188_901)),
            ],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("two launch rows mean the intent launched more than once")
        .to_string();
        assert!(error.contains("2 launch rows"), "{error}");
    }

    #[test]
    fn two_distinct_historical_observations_cannot_share_one_refusal() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let distinct = declaration(188_680, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 2.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 3.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&distinct, CALLER, Some(188_680)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("one refusal after two distinct observations cannot prove both")
        .to_string();
        assert!(error.contains("bound to that exact observation"), "{error}");
        assert!(error.contains("188678"), "{error}");
    }

    #[test]
    fn separately_bound_refusals_release_each_distinct_historical_observation() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let distinct = declaration(188_680, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.5);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 3.0);
        let old_refusal = refusal_data(&old);
        let distinct_refusal = refusal_data(&distinct);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let resolution = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&distinct, CALLER, Some(188_680)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[
                refusal_row(&old_refusal, 188_679, CALLER),
                refusal_row(&distinct_refusal, 188_681, CALLER),
            ],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .expect("each distinct historical observation has its own exact refusal");
        assert_eq!(resolution.superseded_run_seqs, vec![188_678, 188_680]);
        assert_eq!(
            resolution.superseded_refusal_run_seqs,
            vec![188_679, 188_681]
        );
    }

    #[test]
    fn equivalent_historical_duplicates_share_one_refusal() {
        let first = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let duplicate = declaration(188_679, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&first);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let resolution = resolve(
            &[
                row(&first, CALLER, Some(188_678)),
                row(&duplicate, CALLER, Some(188_679)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_680, CALLER)],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .expect("an equivalent re-observation is the same refused declaration");
        assert_eq!(resolution.superseded_run_seqs, vec![188_678, 188_679]);
        assert_eq!(resolution.superseded_refusal_run_seqs, vec![188_680]);
    }

    #[test]
    fn ledger_only_approval_without_current_gate_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            None,
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a ledger answer without the authoritative current gate is not approval")
        .to_string();
        assert!(
            error.contains("requires a durable approved operator decision"),
            "{error}"
        );
    }

    #[test]
    fn a_later_rejected_gate_decision_blocks_supersession() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = format!("tl-terminal-owner-recovery-{SLICE}");
        let approved = approval_data(&gate, "approved");
        let rejected = approval_data(&gate, "rejected");
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &[
                approval_row(&approved, 188_600),
                approval_row(&rejected, 188_650),
            ],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a later rejection revokes the approval")
        .to_string();
        assert!(
            error.contains("approved current supersession is required"),
            "{error}"
        );
    }

    #[test]
    fn a_current_checkpoint_approval_with_a_later_ledger_answer_is_current() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = format!("tl-terminal-owner-recovery-{SLICE}");
        let approved = approval_data(&gate, "approved");
        let resolution = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &[approval_row(&approved, 188_900)],
            expected(),
            Some(&proof),
        )
        .expect("the run/epoch-bound checkpoint is current regardless of event order");
        assert_eq!(
            resolution.supersession_approval,
            Some(DispatchApprovalIdentity {
                source: "checkpoint_and_ledger",
                ledger_answer_run_seq: Some(188_900),
                ..approval_identity(None)
            })
        );
    }

    #[test]
    fn approval_for_another_slice_is_not_proof() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = DispatchCurrentGate {
            gate_name: "tl-terminal-owner-recovery-other-slice".to_string(),
            ..current_gate()
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &[],
            Some(&gate),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("another slice's checkpoint gate does not authorize this supersession")
        .to_string();
        assert!(
            error.contains("is not a recovery gate of the declared scope"),
            "{error}"
        );
    }

    #[test]
    fn unsequenced_declarations_are_refused() {
        let old = declaration(0, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(0, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve(
            &[row(&old, CALLER, None), row(&new, CALLER, None)],
            &[],
            &[],
            &approved_answers(),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("declarations without ledger sequence cannot be ordered")
        .to_string();
        assert!(error.contains("no durable ledger sequence"), "{error}");
    }

    fn approval_identity(ledger_answer_run_seq: Option<u64>) -> DispatchApprovalIdentity {
        DispatchApprovalIdentity {
            gate_name: format!("tl-terminal-owner-recovery-{SLICE}"),
            run_id: CALLER.to_string(),
            controller_epoch: EPOCH.to_string(),
            source: if ledger_answer_run_seq.is_some() {
                "checkpoint_and_ledger"
            } else {
                "checkpoint"
            },
            ledger_answer_run_seq,
        }
    }

    #[test]
    fn resolution_identity_is_stable_and_provenance_specific() {
        let approval = approval_identity(Some(188_600));
        let first = dispatch_authorization_resolution_id(
            INTENT,
            Some(188_878),
            &[188_678],
            &[188_693],
            Some(&approval),
        );
        assert_eq!(
            first,
            dispatch_authorization_resolution_id(
                INTENT,
                Some(188_878),
                &[188_678],
                &[188_693],
                Some(&approval)
            )
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(
                INTENT,
                Some(188_879),
                &[188_678],
                &[188_693],
                Some(&approval)
            )
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(
                INTENT,
                Some(188_878),
                &[188_679],
                &[188_693],
                Some(&approval)
            )
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(
                INTENT,
                Some(188_878),
                &[188_678],
                &[188_694],
                Some(&approval)
            )
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(
                INTENT,
                Some(188_878),
                &[188_678],
                &[188_693],
                Some(&approval_identity(None))
            )
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(
                INTENT,
                Some(188_878),
                &[188_678],
                &[188_693],
                Some(&DispatchApprovalIdentity {
                    controller_epoch: "epoch-other".to_string(),
                    ..approval.clone()
                })
            )
        );
        assert_ne!(
            first,
            dispatch_authorization_resolution_id(
                INTENT,
                Some(188_878),
                &[188_678],
                &[188_693],
                Some(&DispatchApprovalIdentity {
                    run_id: "run-other".to_string(),
                    ..approval.clone()
                })
            )
        );
    }

    /// The real #1511 shape: an approved checkpoint with no `tl.gate_answered`
    /// row and a selected declaration that predates any new event.
    #[test]
    fn checkpoint_only_approval_releases_the_historical_supersession() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let resolution = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &[],
            expected(),
            Some(&proof),
        )
        .expect("the current checkpoint approval is durable without a fabricated ledger event");
        assert_eq!(resolution.selected_run_seq, Some(188_878));
        assert_eq!(
            resolution.supersession_approval,
            Some(approval_identity(None))
        );
    }

    #[test]
    fn missing_current_gate_state_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &[],
            None,
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a missing current gate cannot authorize a supersession")
        .to_string();
        assert!(
            error.contains("requires a durable approved operator decision"),
            "{error}"
        );
    }

    #[test]
    fn reopened_pending_gate_blocks_the_older_approval() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = DispatchCurrentGate {
            status: "pending".to_string(),
            ..current_gate()
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            Some(&gate),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a reopened pending gate revokes the older approval")
        .to_string();
        assert!(error.contains("is pending"), "{error}");
    }

    #[test]
    fn rejected_checkpoint_gate_blocks_a_stale_ledger_approval() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = DispatchCurrentGate {
            status: "rejected".to_string(),
            ..current_gate()
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            Some(&gate),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("the checkpoint rejection wins over a stale emitted approval")
        .to_string();
        assert!(
            error.contains("approved current supersession is required"),
            "{error}"
        );
    }

    #[test]
    fn a_pending_other_same_slice_recovery_gate_blocks_supersession() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = DispatchCurrentGate {
            other_recovery_gates: vec![(
                format!("tl-dispatch-failed-{SLICE}"),
                "pending".to_string(),
            )],
            ..current_gate()
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            Some(&gate),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("another pending same-slice gate means the approval is not current")
        .to_string();
        assert!(
            error.contains("another same-slice recovery gate"),
            "{error}"
        );
        assert!(error.contains("is pending"), "{error}");
    }

    #[test]
    fn a_ledger_rejection_conflicting_with_the_checkpoint_is_refused() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = format!("tl-terminal-owner-recovery-{SLICE}");
        let rejected = approval_data(&gate, "rejected");
        let error = resolve(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &[approval_row(&rejected, 188_650)],
            expected(),
            Some(&proof),
        )
        .err()
        .expect("contradictory ledger evidence must fail closed")
        .to_string();
        assert!(
            error.contains("conflicts with the current durable checkpoint"),
            "{error}"
        );
    }

    #[test]
    fn a_stale_controller_epoch_is_not_current_provenance() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = DispatchCurrentGate {
            controller_epoch: "epoch-stale".to_string(),
            ..current_gate()
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            Some(&gate),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a stale controller epoch cannot prove the current gate")
        .to_string();
        assert!(error.contains("controller epoch"), "{error}");
    }

    #[test]
    fn a_gate_owned_by_another_run_is_not_current_provenance() {
        let old = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
        let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
        let old_refusal = refusal_data(&old);
        let owner = predecessor();
        let proof = no_effect(&owner);
        let gate = DispatchCurrentGate {
            run_id: "foreign-run".to_string(),
            ..current_gate()
        };
        let error = resolve_dispatch_declaration(
            &[
                row(&old, CALLER, Some(188_678)),
                row(&new, CALLER, Some(188_878)),
            ],
            &[],
            &[refusal_row(&old_refusal, 188_693, CALLER)],
            &approved_answers(),
            Some(&gate),
            expected(),
            Some(&proof),
        )
        .err()
        .expect("a gate owned by another run cannot authorize this declaration")
        .to_string();
        assert!(error.contains("not attributed to the run"), "{error}");
    }

    fn write_checkpoint(root: &Path, run_id: &str, document: Value) {
        let directory = root.join(".exo/tl-loop/root").join(run_id);
        fs::create_dir_all(&directory).expect("create checkpoint directory");
        fs::write(
            directory.join("run.json"),
            serde_json::to_vec(&document).expect("serialize checkpoint"),
        )
        .expect("write checkpoint");
    }

    fn checkpoint_document(run_id: &str, gates: Value) -> Value {
        let mut slices = serde_json::Map::new();
        slices.insert(SLICE.to_string(), json!({"status": "spawned"}));
        json!({
            "run_id": run_id,
            "controller_epoch": EPOCH,
            "gates": gates,
            "slices": slices,
        })
    }

    #[test]
    fn reads_the_current_approved_gate_from_the_controller_checkpoint() {
        let temporary = tempfile::tempdir().expect("temp dir");
        write_checkpoint(
            temporary.path(),
            CALLER,
            checkpoint_document(
                CALLER,
                json!([{"name": format!("tl-terminal-owner-recovery-{SLICE}"), "status": "approved"}]),
            ),
        );
        let gate = read_current_recovery_gate(temporary.path(), SLICE)
            .expect("the approved checkpoint is current");
        assert_eq!(gate.run_id, CALLER);
        assert_eq!(gate.controller_epoch, EPOCH);
        assert_eq!(
            gate.gate_name,
            format!("tl-terminal-owner-recovery-{SLICE}")
        );
        assert_eq!(gate.status, "approved");
        assert!(gate.other_recovery_gates.is_empty());
    }

    #[test]
    fn reads_a_pending_dispatch_failed_gate_as_other_current_state() {
        let temporary = tempfile::tempdir().expect("temp dir");
        write_checkpoint(
            temporary.path(),
            CALLER,
            checkpoint_document(
                CALLER,
                json!([
                    {"name": format!("tl-terminal-owner-recovery-{SLICE}"), "status": "approved"},
                    {"name": format!("tl-dispatch-failed-{SLICE}"), "status": "pending"}
                ]),
            ),
        );
        let gate = read_current_recovery_gate(temporary.path(), SLICE).expect("checkpoint");
        assert_eq!(gate.status, "approved");
        assert_eq!(
            gate.other_recovery_gates,
            vec![(format!("tl-dispatch-failed-{SLICE}"), "pending".to_string())]
        );
    }

    #[test]
    fn refuses_an_ambiguous_checkpoint_set() {
        let temporary = tempfile::tempdir().expect("temp dir");
        let document = checkpoint_document(
            CALLER,
            json!([{"name": format!("tl-terminal-owner-recovery-{SLICE}"), "status": "approved"}]),
        );
        write_checkpoint(temporary.path(), CALLER, document.clone());
        write_checkpoint(temporary.path(), "another-run", document);
        let error = read_current_recovery_gate(temporary.path(), SLICE)
            .err()
            .expect("two checkpoints cannot both own the current gate")
            .to_string();
        assert!(error.contains("expected exactly one"), "{error}");
    }

    #[test]
    fn refuses_a_gate_checkpoint_that_does_not_own_the_scope() {
        let temporary = tempfile::tempdir().expect("temp dir");
        write_checkpoint(
            temporary.path(),
            CALLER,
            json!({
                "run_id": CALLER,
                "controller_epoch": EPOCH,
                "gates": [{"name": format!("tl-terminal-owner-recovery-{SLICE}"), "status": "approved"}],
                "slices": {},
            }),
        );
        let error = read_current_recovery_gate(temporary.path(), SLICE)
            .err()
            .expect("a checkpoint without the slice cannot own its gate")
            .to_string();
        assert!(error.contains("does not own scope"), "{error}");
    }

    #[test]
    fn refuses_a_missing_checkpoint() {
        let temporary = tempfile::tempdir().expect("temp dir");
        let error = read_current_recovery_gate(temporary.path(), SLICE)
            .err()
            .expect("no checkpoint means no current gate")
            .to_string();
        assert!(
            error.contains("no durable controller checkpoint"),
            "{error}"
        );
    }

    #[test]
    fn missing_observation_fields_are_not_merged_as_equivalent() {
        for field in [
            "started_at",
            "attempt",
            "dispatch_generation",
            "controller_epoch",
        ] {
            let mut first = declaration(188_678, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
            let mut duplicate =
                declaration(188_679, INTENT, CALLER, SLICE, OLD_CONFIG, OLD_ROUTE, 1.0);
            first.as_object_mut().expect("object").remove(field);
            duplicate.as_object_mut().expect("object").remove(field);
            let new = declaration(188_878, INTENT, CALLER, SLICE, NEW_CONFIG, NEW_ROUTE, 2.0);
            let old_refusal = refusal_data(&first);
            let owner = predecessor();
            let proof = no_effect(&owner);
            let error = resolve(
                &[
                    row(&first, CALLER, Some(188_678)),
                    row(&duplicate, CALLER, Some(188_679)),
                    row(&new, CALLER, Some(188_878)),
                ],
                &[],
                &[refusal_row(&old_refusal, 188_680, CALLER)],
                &[],
                expected(),
                Some(&proof),
            )
            .err()
            .unwrap_or_else(|| panic!("missing {field} must not collapse distinct observations"))
            .to_string();
            assert!(
                error.contains("has no durable verified prelaunch configuration refusal"),
                "{field}: {error}"
            );
        }
    }
}

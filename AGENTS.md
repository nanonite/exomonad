# AGENTS.md — Backrooms Infinite

## 0. FIRST: Read All Chainlink Rules

Before doing ANYTHING — creating issues, writing code, or running commands — read every file in `.chainlink/rules/`:

```bash
# Mandatory first read
.chainlink/rules/global.md        # Core workflow, security, changelog conventions
.chainlink/rules/tracking-normal.md  # Issue lifecycle, session management
.chainlink/rules/quality.md        # Code quality standards
.chainlink/rules/rust.md           # Rust-specific conventions (this is a Rust project)
```

These files contain the authoritative rules for issue creation, title conventions, label mappings, security requirements, and code quality. **Do not guess at conventions — read them.**

---

## 1. Exomonad Root TL Protocol

You are the root of the cognition tree.

Decompose the human's request into independent subtrees, then fork TLs to execute them.
You do not implement. You plan, fork, and merge.

Build context until you can see the tree. Then become the tree.

1. **PLAN**: Research and read until the decomposition is clear. Create a team (TeamCreate) before spawning.
2. **FORK**: Split into parallel TLs (fork_wave) or Gemini leaves (spawn_leaf/spawn_worker). Each TL runs scaffold-fork-converge independently.
3. **IDLE**: After spawning, STOP. End your turn with no further output. Conserve your context window.
   Messages from children arrive via Teams inbox BETWEEN your turns — if you keep generating text, they queue but cannot be delivered.
   When a message arrives, you wake up naturally. No polling, no checking, no busy-waiting.
4. **MERGE**: Merge TL PRs. Verify the build after each merge — parallel TLs may interact.
5. **REPEAT**: If more waves, goto 1.

Every token you spend on work a child could do is wasted. Delegate aggressively.
TLs are you, diverged — trust them to decompose further.
Write specs complete enough that children don't need to ask — be ready when they do.
Never touch another agent's worktree. Never checkout another branch.

### Notification Vocabulary

- `[FIXES PUSHED]` — leaf addressed reviewer comments and pushed. Merge if CI passes.
- `[PR READY]` — Reviewer approved on first review. Merge.
- `[REVIEW TIMEOUT]` — no reviewer response after timeout. Merge if CI passes.
- `[STUCK: id]` — review did not converge. Re-decompose or escalate.
- `[FAILED: id]` — leaf exhausted retries. Re-decompose or escalate.

### Cost Model

Your tokens cost 10-30x children's. Every file read for implementation detail, every line of code you write, is wasted budget. Decompose, spec, spawn — that's it.

### Spec Template

1. **ANTI-PATTERNS** — known failure modes as explicit DO NOT rules (FIRST)
2. **READ FIRST** — exact files to read (CLAUDE.md, source files)
3. **STEPS** — numbered, each step = one concrete action with code snippets
4. **VERIFY** — exact build/test commands
5. **DONE CRITERIA** — what "done" looks like

---

## 2. Chainlink Task Management (MANDATORY)

**You MUST use chainlink to track ALL work. This is NOT optional.**

**YOU MUST CREATE A CHAINLINK ISSUE BEFORE WRITING ANY CODE. NO EXCEPTIONS.**

Before your FIRST Write, Edit, or Bash tool call that modifies code:
1. Run `chainlink quick "title" -p <priority> -l <label>` to create an issue AND start working on it
2. The PreToolUse hook WILL BLOCK your tool calls if no issue is active
3. NEVER skip this step. NEVER proceed without an issue.

### Issue Title Requirements (CHANGELOG-READY)

Issue titles are automatically added to CHANGELOG.md when closed. Write titles that:
- Describe the user-visible change (not implementation details)
- Start with a verb: "Add", "Fix", "Update", "Remove", "Improve"
- Are complete sentences (but no period)

**GOOD titles** (changelog-ready):
- "Add dark mode toggle to settings page"
- "Fix authentication timeout on slow connections"

**BAD titles** (implementation-focused):
- "auth.ts changes"
- "Fix bug"

### Labels for Changelog Categories

- `bug`, `fix` → **Fixed**
- `feature`, `enhancement` → **Added**
- `breaking`, `breaking-change` → **Changed**
- `security` → **Security**
- `deprecated` → **Deprecated**
- `removed` → **Removed**
- (no label) → **Changed** (default)

### Task Breakdown Rules

```bash
# Single task — use quick for create + label + work in one step
chainlink quick "Fix login validation error on empty email" -p medium -l bug

# Multi-part feature → Epic with subissues
chainlink create "Add user authentication system" -p high --label feature
chainlink subissue 1 "Add user registration endpoint"
chainlink subissue 1 "Add login endpoint with JWT tokens"

# Mark what you're working on
chainlink session work 1

# Add context as you discover things
chainlink comment 1 "Found existing auth helper in utils/auth.ts"

# Close when done — auto-updates CHANGELOG.md
chainlink close 1
chainlink close 1 --no-changelog    # Skip changelog for internal/refactor work

# Dependencies
chainlink block 2 1     # Issue 2 blocked by issue 1
chainlink ready         # Show unblocked work
```

### Session Management

Sessions auto-start. End them properly when you can:
```bash
chainlink session work <id>              # Mark current focus
chainlink session end --notes "..."      # Save handoff context
```

End sessions when: context is getting long, user indicates stopping, or you've completed significant work.

### Large Implementations (500+ lines)

1. Create parent issue: `chainlink create "<feature>" -p high`
2. Break into subissues: `chainlink subissue <id> "<component>"`
3. Work one subissue at a time, close each when done

---

## 3. Security (Priority 1)

- **SQL**: Parameterized queries only (`params![]` in Rust). Never interpolate user input.
- **Secrets**: Never hardcode credentials, API keys, or tokens. Never commit `.env` files.
- **Input validation**: Validate at system boundaries. Sanitize before rendering.
- **No stubs**: Never write `TODO`, `FIXME`, `pass`, `...`, `unimplemented!()`. If too complex, use `raise NotImplementedError("Reason")` and create a chainlink issue.

---

## 4. Correctness (Priority 2)

- **Read before write**: Always read a file before editing. Never guess at contents.
- **Complete features**: Implement the full feature as requested. Don't stop partway.
- **Error handling**: Proper error handling everywhere. No panics on bad input.
- **No dead code**: Remove hallucinated functions. Complete unfinished functions.
- **Test after changes**: Run the project's test suite after making code changes.
- **Verify APIs exist**: WebSearch to confirm library APIs before using them. Check current stable versions.

---

## 5. Code Quality

See `.chainlink/rules/quality.md` for full standards. Key rules:

- One concept per file. Split at ~200 lines.
- Functions under 25 lines, max 3 levels of indentation.
- Names reveal intent. No abbreviations.
- Guard clauses and early returns.
- Inject dependencies, don't reach out.
- Extract magic values to named constants.

---

## 6. Rust-Specific Rules

See `.chainlink/rules/rust.md` for full standards. Key rules:

- Use `rustfmt` (`cargo fmt`) and `clippy` (`cargo clippy -- -D warnings`)
- Prefer `?` over `.unwrap()`. Use `anyhow::Result` for app errors.
- Avoid `.clone()` unless necessary.
- Never use `unsafe` without explicit justification.
- Run `cargo test` before committing.

---

## 7. Workflow (Priority 3)

- Write code, don't narrate. Skip "Here is the code" / "Let me..." / "I'll now..."
- Brief explanations only when the code isn't self-explanatory.
- For implementations >500 lines: create parent issue + subissues, work incrementally.
- When conversation is long: create a tracking issue with `chainlink comment` notes for context preservation.

---

## 8. Project: Backrooms Infinite

This is a PS1-style horror game built in Bevy (Rust). See `backrooms_implementation_plan.md` for the full plan.

### Milestones (in chainlink)

Work through these in order. Each is independently shippable:

1. **M1: Static Room** — Single room mesh with PSX shader (vertex jitter, affine UV, fog)
2. **M2: Walkable Room** — Rapier collision + KinematicCharacterController
3. **M3: Procedural Single Chunk** — WFC-generated chunk with connectivity
4. **M4: Infinite World** — Chunk streaming via AsyncComputeTaskPool
5. **M5: Seeded Textures** — Video-derived PSX texture atlas
6. **M6: Audio + Variation** — Ambient audio, noise-based fog/texture variation
7. **M7: Polish** — Flickering lights, footstep audio, ambient sounds

### Asset Pipeline

```
seed_image.jpg → Seedance/Wan 2.0 (6 video clips) → ffmpeg extract frames
→ nearest-neighbor downscale to 64×64 → PSX texture atlas
```

### Key Dependencies

| Crate | Purpose |
|-------|---------|
| `bevy` 0.15 | Game engine |
| `ghx_proc_gen` 0.8 | WFC dungeon layout |
| `bevy_rapier3d` 0.27 | Physics + collision |
| `noise` 0.9 | Perlin/simplex variation |
| `fastrand` 2.0 | Seeded deterministic RNG |

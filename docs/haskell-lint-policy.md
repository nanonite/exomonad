# Haskell lint policy

`just lint` gates owned Haskell source under `haskell/wasm-guest`. It compares
HLint 3.10 findings with `scripts/hlint-owned-baseline.json`, captured from
pre-#1280 commit `d60bc52869ea60a0383f43c742d86abec3f679e3`. The baseline uses a
multiset of file, hint name, source expression, and suggested replacement. A
new finding fails the gate; resolved baseline findings do not need a baseline
rewrite. The checker has no automatic baseline-update mode, and a HLint version
change fails until the baseline is explicitly reviewed.

Generated protobuf files in `haskell/proto` and third-party sources in
`haskell/vendor` are separate from owned source and excluded from this gate.
With HLint 3.10, the current raw inventory is 2,729 generated findings, 430
vendored findings, and 94 owned-source baseline findings. They are not combined
into the owned-source baseline.

To inspect every scope directly, run `hlint haskell`; that command reports the
full inventory and exits nonzero while any hints exist. `just lint` is the
change gate for owned code and fails when an unbaselined owned-source hint is
introduced.

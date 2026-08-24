#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

OUTPUT_ROOT="${PROTO_OUTPUT_ROOT:-haskell/proto/src}"

echo ">>> Generating Haskell types from proto..."

# Requires: run from within `nix develop` which provides compile-proto-file
if ! command -v compile-proto-file &> /dev/null; then
    echo "ERROR: compile-proto-file not found."
    echo "Run this script from within 'nix develop' shell."
    exit 1
fi

COMPILE="compile-proto-file"

# Clean and regenerate (preserve Compat.hs - it's hand-written)
echo ">>> Cleaning generated files..."
for f in "$OUTPUT_ROOT"/ExoMonad/*.hs; do
    base=$(basename "$f")
    if [[ "$base" != "Compat.hs" ]]; then
        rm -f "$f"
    fi
done
rm -rf "$OUTPUT_ROOT/ExoMonad/Effects"

mkdir -p "$OUTPUT_ROOT/ExoMonad"
mkdir -p "$OUTPUT_ROOT/ExoMonad/Effects"

# Generate from core proto files
for proto in proto/exomonad/*.proto; do
    proto_rel="${proto#proto/}"
    echo "    Processing: $proto_rel"
    $COMPILE --includeDir proto --proto "$proto_rel" --out "$OUTPUT_ROOT"
done

# Generate from effects proto files
for proto in proto/effects/*.proto; do
    proto_rel="${proto#proto/}"
    echo "    Processing: $proto_rel"
    $COMPILE --includeDir proto --proto "$proto_rel" --out "$OUTPUT_ROOT"
done

echo ">>> Post-processing generated files..."

# Process all generated files (core + effects)
find "$OUTPUT_ROOT" -name '*.hs' | while read -r f; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f")
    [[ "$base" == "Compat.hs" ]] && continue

    # Strip ToSchema instances (not compatible with WASM, requires swagger)
    echo "    Stripping ToSchema from: $f"
    perl -i -0pe 's/instance \(HsJSONPB\.ToSchema[^}]+\}[^}]*\}\n?//gs' "$f"
    # Remove any stray closing braces left after stripping
    perl -i -pe 's/^}\n?// if /^}$/' "$f"

    # Strip gRPC imports and service code (not compatible with WASM)
    # Remove gRPC import blocks (including multi-line continuations like "hiding (serverLoop)")
    perl -i -0pe 's/import Network\.GRPC\.HighLevel\.\S+.*?(?=\n(?:import |newtype |data |class ))//gs' "$f"
    # Remove everything from the service record to end of file
    # (service records are always at the end, after all message types)
    perl -i -0pe 's/\n(?:data|newtype) \w+Effects request response\b.*\z/\n/s' "$f"

    # Fix module names: Exomonad -> ExoMonad (proto3-suite lowercases)
    sed -i.bak 's/Exomonad\./ExoMonad./g' "$f"

    rm -f "${f}.bak"
done

# Move files from Exomonad/ to ExoMonad/ if proto3-suite created wrong directory.
# On macOS (case-insensitive FS), Exomonad/ and ExoMonad/ are the same dir —
# the sed fixup already corrected module names in-place, so mv is a no-op.
# We test with a temp file to detect case-insensitive FS.
if [ -d "$OUTPUT_ROOT/Exomonad" ]; then
    _test_file="$OUTPUT_ROOT/Exomonad/.case_test_$$"
    touch "$_test_file"
    if [ -f "$OUTPUT_ROOT/ExoMonad/.case_test_$$" ]; then
        # Case-insensitive: same directory, skip move
        echo ">>> Case-insensitive FS detected, skipping Exomonad → ExoMonad move"
        rm -f "$_test_file"
    else
        # Case-sensitive: actually need to move
        rm -f "$_test_file"
        echo ">>> Moving files from Exomonad/ to ExoMonad/..."
        for f in "$OUTPUT_ROOT"/Exomonad/*.hs; do
            [[ -f "$f" ]] || continue
            mv "$f" "$OUTPUT_ROOT/ExoMonad/$(basename "$f")"
        done
        if [ -d "$OUTPUT_ROOT/Exomonad/Effects" ]; then
            mkdir -p "$OUTPUT_ROOT/ExoMonad/Effects"
            for f in "$OUTPUT_ROOT"/Exomonad/Effects/*.hs; do
                [[ -f "$f" ]] || continue
                mv "$f" "$OUTPUT_ROOT/ExoMonad/Effects/$(basename "$f")"
            done
            rmdir "$OUTPUT_ROOT/Exomonad/Effects" 2>/dev/null || true
        fi
        rmdir "$OUTPUT_ROOT/Exomonad" 2>/dev/null || true
    fi
fi

# proto3-suite can place effect modules below the core namespace when the
# output tree already contains generated core modules.  The package and the
# tracked tree expose these modules as Effects.*, so normalize that path after
# every generation instead of letting a seeded output tree drift.
if [ -d "$OUTPUT_ROOT/ExoMonad/Effects" ]; then
    mkdir -p "$OUTPUT_ROOT/Effects"
    for f in "$OUTPUT_ROOT"/ExoMonad/Effects/*.hs; do
        [[ -f "$f" ]] || continue
        mv "$f" "$OUTPUT_ROOT/Effects/$(basename "$f")"
    done
    rmdir "$OUTPUT_ROOT/ExoMonad/Effects" 2>/dev/null || true
fi

echo ">>> Formatting generated Haskell with Ormolu..."
mapfile -t generated_haskell < <(find "$OUTPUT_ROOT" -name '*.hs' -print)
if ((${#generated_haskell[@]} > 0)); then
    ormolu --mode inplace --ghc-opt -XImportQualifiedPost "${generated_haskell[@]}"
fi

echo ">>> Generated files:"
ls -la "$OUTPUT_ROOT"/ExoMonad/*.hs
if ls "$OUTPUT_ROOT"/Effects/*.hs &>/dev/null; then
    ls -la "$OUTPUT_ROOT"/Effects/*.hs
fi

echo ""
echo ">>> Done! Remember to commit the generated files."
echo ""
echo "Next steps:"
echo "  1. cabal build exomonad-proto   # Verify Haskell builds"
echo "  2. cargo build -p exomonad-proto # Verify Rust builds"
echo "  3. just proto-test               # Run wire format tests"

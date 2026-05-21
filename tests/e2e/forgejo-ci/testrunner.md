# Forgejo CI Testrunner Checklist

- `run.sh` exits with status 0.
- `.github/workflows/ci.yml` exists in the generated workspace.
- `git remote` includes `forgejo`.
- `new.log` contains Forgejo registration output (or explicit graceful fallback).
- Future hard assertion target: verify `/ci` webhook receives workflow status and emits `[MERGE READY]`.

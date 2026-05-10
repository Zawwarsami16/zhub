# PR

**What this changes**
One sentence on the user-visible effect.

**Why**
The use case, bug, or design pressure motivating it.

**Substrate-alignment check**
☐ This stays substrate-neutral (no coupling to a specific AI, device, or product)
☐ Or: this is a doc / tooling / test change that doesn't affect the substrate surface

**Test discipline**
☐ A failing test was written first (TDD red), then made pass
☐ Or: this is doc-only / no behavior change

**Verifications run**
- `pytest` → <NN/NN passed>
- `cd js && npm test` (if JS touched) → <NN/NN passed>
- `python -m zhub doctor` (if install / CLI touched) → clean

**Spec / plan** (for medium and above)
If this implements a feature, link the `docs/superpowers/specs/...` design doc.
For tiny PRs (typo, regression test, one-line fix), this isn't required.

---
name: Feature request
about: Propose a new substrate primitive, brain adapter, or improvement
title: "[feature] "
labels: enhancement
---

**The problem**
What use case or scenario is currently awkward or impossible?

**Proposed solution**
Sketch the API or wire-shape change. Stay neutral — zhub is a substrate, not a product.

**Alternatives considered**
What other approaches did you think about? Why is this one better?

**Scope check**
Is this small (a flag / endpoint), medium (a new module), or large (a new primitive)?
For medium and above, please open a `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
PR first so the design can be reviewed before implementation.

**Substrate-alignment check**
Does this couple zhub to a specific product, AI, or device? If yes, the bridge
likely belongs in that product's own repo, consuming zhub via `publish()` /
`connect()` / `expose()`.

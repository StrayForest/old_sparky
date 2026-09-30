# Retained profile semantics

These files preserve historical contracts used by retained load evidence.
They include the pre-separation v1 read/Ready Vote contracts and the retired
Ready Vote saturation sweeps (`ready-vote-saturation-ramp-v1` through `v3`).
They are intentionally outside the active top-level profile registry and are
not selectable by the production workflow. New measurements must use the
versioned profiles in the parent directory so historical reports cannot be
mistaken for the corrected SLO, capacity or stress model.

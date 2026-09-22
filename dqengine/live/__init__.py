"""The single-account live stack (open since 2026-09-18, spec §4): order
book, capabilities, determinism check, second-bar consolidation, the
notification bus, and `driver` -- everything that ticks one deployment,
from a bar event to the published intent (Phase 2). The executor core
joins in Phase 3."""

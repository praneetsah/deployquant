"""The single-deployment live driver.

Everything that ticks ONE deployment -- resolve its code, build and step its
warm engine, fall back to a replay, prime scheduled fires off quotes, audit at
the roll, write the payload, wake on bar events, publish the intent -- lives
here (spec 2026-09-19 §3.6).

Five modules. `ports` is the three things the driver cannot do for itself:
the rows it reads and writes, the bars it puts where the sandbox can read
them, and who is told that a tick may have moved money. `deployment` is the
leaf every other module reads -- session clock, column readers, the source a
deployment ticks on. `engine` is the warm engine and the replay behind it,
`tick` is one tick of one deployment, and `loop` is the process that wakes
on bar events and publishes the intent.
"""

"""DQengine: a LEAN-compatible Python trading engine.

`dqengine.runtime` is the engine (algorithm surface, fills, calendar,
backtester, warm live engine); around it sit the bar feed and store, the
broker adapter interface and plugin loader, the single-account live stack
and the algorithm sandbox. The hosted platform built on this installs the
same package; nothing here exists only for self-hosters."""
__version__ = "0.1.0"

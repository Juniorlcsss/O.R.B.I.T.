"""GEAP simulation layer for Project O.R.B.I.T.

Simulates the enterprise services of a Google Enterprise Agent Platform-style
deployment so the fleet's hardened behaviours are demonstrable without the
managed SaaS control plane:

* ``memory_bank``    — Firestore-backed persistent satellite state / context.
* ``agent_registry`` — discoverable catalogue of sub-agents for the router.
* ``model_armor``    — deterministic middleware that intercepts tool calls,
                       logs violations to an audit trail and blocks them.
"""

"""SMTP verification layer of the Email Intelligence Engine (docs/EMAIL_ENGINE.md §SMTP rules).

* ``classify``       — RFC 3463-first reply classification (accepted / rejected / temporary / blocked / unknown)
                       and session outcomes (infrastructure vs mailbox).
* ``health``         — health of OUR SMTP path (global + per provider): sliding window, BLOCKED cooldown,
                       half-open canary, per-provider circuit breaker.
* ``session``        — batched per-domain prober (one logical session per domain, ≤ 3 targets per connection,
                       random catch-all probes, MX walking, STARTTLS, per-MX-host serialisation).
* ``deep_verifiers`` — ``DeepVerifier`` protocol: builtin (port 25), service (Go/AfterShip), simulated world.
* ``world``          — deterministic simulated mail world for tests and benchmarks (no I/O).
* ``canary``         — optional canary probes against known non-catch-all domains.

Submodules are imported explicitly (this package imports nothing eagerly, to stay cycle-free with
``scout.email.verifier``).
"""

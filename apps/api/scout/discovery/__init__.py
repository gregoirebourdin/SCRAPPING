"""Company discovery: source adapters behind ``scout.discovery.base.DiscoverySource``.

Entry points for the pipeline: ``router.select_sources`` / ``router.get_source`` (which adapters, in which
order), ``adapter.plan(defn, expansion=n)`` (cumulative, stable query keys) and ``adapter.discover(query,
cursor)``; ``health.record_request`` / ``health.record_outcomes`` / ``health.health_snapshot`` for source
health; ``catalog.seed_sources`` / ``catalog.source_quality`` for the ``sources`` table.
"""

"""Benchmark harness (docs/ARCHITECTURE.md §Benchmark Harness).

Ground-truth datasets are compared with what the engine has (registry mode), what it produces now
(live mode) or what a synthetic suite measures (suite mode). Only measured numbers are reported — with
their sample size and a Wilson 90 % interval — never a comparison claim against another product.
"""

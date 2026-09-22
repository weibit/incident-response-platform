---
id: timeout-regression
title: Diagnose an upstream timeout regression
service: checkout
tags: [timeouts, configuration]
updated: 2026-08-14
---

Upstream timeout regressions usually appear as a sudden rise in errors with no change
in upstream saturation. The upstream is healthy; the caller stopped waiting long enough.

## Evidence to gather

Compare the effective client timeout across the last two releases in the configuration
diff. Check whether upstream latency actually moved, or only the caller's patience.

## Common cause

A timeout value reduced by an order of magnitude during a release, for example from
five seconds to one hundred milliseconds, turns normal upstream latency into errors.

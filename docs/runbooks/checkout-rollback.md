---
id: checkout-rollback
title: Roll back a bad checkout release
service: checkout
tags: [rollback, deployment]
updated: 2026-09-01
---

Use this runbook when a recent checkout release correlates with elevated error rate
or latency, and no configuration change outside the release explains the regression.

## Preconditions

Confirm a release landed within the degradation window, and that the previous release
was healthy. A rollback without a healthy prior release is not a mitigation.

## Steps

Roll the deployment back to the previous release. This action requires approval
because it is classified as high risk. Do not roll back and restart in the same change.

## Verification

Observe error rate below 1 percent and p95 latency below 500 milliseconds for five
minutes after the rollback completes. A single healthy sample is not verification.

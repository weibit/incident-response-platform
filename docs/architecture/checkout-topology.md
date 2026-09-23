# Checkout service topology

Checkout calls payments synchronously with a 5 second upstream timeout. Payments calls the
ledger asynchronously. A timeout budget change in checkout therefore surfaces as payments
errors before it surfaces as ledger errors.

Ownership sits with commerce-oncall. The service is deployed from the commerce monorepo and
shares a connection pool with the cart service, so pool exhaustion in either appears in both.

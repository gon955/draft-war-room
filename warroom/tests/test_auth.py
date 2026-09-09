"""Registration, login and token handling (SPEC 6).

  register -> login -> GET /auth/me returns that user
  a protected route without a token -> 401
  registering a duplicate email -> 409
  emails are stored lowercased, so Alice@x.com and alice@x.com collide

TODO (Phase 2).
"""

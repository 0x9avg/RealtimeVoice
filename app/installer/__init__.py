"""Guided setup wizard for a non-technical customer.

This package is a SEPARATE small FastAPI app (`app.installer.server:app`,
its own uvicorn process on its own port — see `deploy/realtimevoice-setup.
service`) from the main voice pipeline (`app.main:app`). Two reasons it is
not just another router mounted on `app.main`:

1. It must stay reachable even when the main voice service is crashlooping
   or has never been started at all — that's exactly the situation a
   customer opens it to diagnose/fix.
2. It edits `.env` and restarts/stops the main service via `systemctl
   --user` — giving that power to the same process a browser tab drives a
   live Gemini session through would be an unnecessary blast-radius
   increase for no benefit.

See `installer/bootstrap.sh` for the one command a customer/technician runs
ONCE to get from a fresh checkout to this wizard being reachable in a
browser, and `DEPLOYMENT.md`'s "Быстрая установка через мастер" section for
the customer-facing walkthrough.
"""

from __future__ import annotations

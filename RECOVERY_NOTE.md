# Safe Recovery

This package restores the latest uploaded working single-file app.py as the active app.

Why:
- The real split caused missing/duplicate route registration.
- Render logs showed missing endpoint `home`.
- This recovery brings all original routes back from app.py.

Next migration must be done one route at a time and tested locally before deploy.

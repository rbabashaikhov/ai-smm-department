"""HTTP control plane.

The factory lives in ai_smm.api.app. It is not imported here: importing
the package must not pull in FastAPI for a CLI or worker process that has
no use for it.
"""

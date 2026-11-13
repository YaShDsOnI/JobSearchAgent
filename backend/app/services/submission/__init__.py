"""Phase 6 application adapter. The only destination is the local sandbox ATS.

Design: docs/application-agent.md, "Phase 6: sandbox browser adapter". Only
`browser.py` imports a browser library; it is used by the isolated runner process
(`python -m app.runner`), never by the API.
"""

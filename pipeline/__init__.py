"""Offline ingestion/scoring/digest/analytics pipeline.

Runs as a scheduled job (never inside the Space). Orchestrated by run_weekly.py:
harvest → normalize → score → digest → analytics.
"""

"""Candidate identity: the person behind applications (docs/identity_design.md).

``phones`` (canonical phone numbers, withheld caller IDs) has no dependencies;
``service`` (candidates, keys, merges, per-channel facts) works through ``storage/db.py``.
"""

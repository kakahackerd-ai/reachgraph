"""File-level code graph enrichment for Flow 2: which files import which
declared external dependencies (import_scan.py, this project's own static
scanner -- see its module docstring for why), and the local intra-repo
file/function call graph (gitnexus_client.py, a real integration of the
external `gitnexus` CLI). These are complementary, not overlapping:
gitnexus's IMPORTS/CALLS/DEFINES graph is scoped entirely to files inside
the analyzed repo (confirmed by hand -- it does not create a node for an
external package even with node_modules installed, and `gitnexus impact
<external-package-name>` returns "Target not found"), so it cannot answer
"which files import package X" on its own.
"""

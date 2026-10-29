"""Second Opinion: an evidence auditor for Qloo entity recommendations.

The package deliberately contains no composite scoring function. It reports
per-dimension evidence and a discrete audit state, because collapsing evidence
into a single number is the failure mode the tool exists to expose.
"""

__version__ = "1.0.0"

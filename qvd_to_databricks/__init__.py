"""QVD to Databricks migration helpers.

Step 1 intentionally reads only QVD metadata headers. Row-level reading and
Delta/Parquet conversion belong to later steps.
"""


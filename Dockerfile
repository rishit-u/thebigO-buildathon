# Same base image the organizers labeled with (pinned by digest).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The selector needs only the standard library and the prebuilt map.
# Optional: the labeling environment's test dependencies (requirements.lock versions) from PyPI.
# Short timeouts so a build without network moves on quickly; then the stub runner list is kept.
RUN pip install --no-cache-dir --timeout 15 --retries 1 duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 \
        python-dateutil pytz tzdata six || echo "test dependencies not installed"
COPY selector.py map.json.gz testmap.json.gz tpcds_fp.py tpcds_res.py maxnodes_fp.py /app/
COPY rootcfg /app/rootcfg
COPY tpcds_data /app/tpcds_data
# in-container runner: the pinned test suite and the test list (to find the tests runnable here)
COPY runner.py tests.json runner_pinned.json runner_pinned_deps.json tpcds_perq.json /app/
# with the real dependencies importable, use the runnable list built with them (1617 vs 1390 tests)
RUN python -c "import numpy, pandas, pandas.testing, duckdb" && cp /app/runner_pinned_deps.json /app/runner_pinned.json || echo "stub runner list"
COPY tests_pinned /app/pinned/tests
# TPC-DS stage fingerprints of the pinned commit, computed here in the scoring image itself:
# writes /app/pinned (pinned sqlglot from the map), /app/tpcds_spec.json, /app/tpcds_pinned.json.
# If the two build runs disagree, no fingerprints are written and the check is off.
RUN python /app/selector.py --build-pinned
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]

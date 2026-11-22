# Same base image the organizers labeled with (pinned by digest).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The selector needs only the standard library and the prebuilt map.
# The labeling environment's test dependencies (requirements.lock versions), installed offline from
# shipped wheels (x86_64 and aarch64). If this fails the selector still works: tests that need them
# are then reported failing by the in-container runner and simply stay selected.
COPY wheels /tmp/wheels
RUN pip install --no-index --find-links /tmp/wheels duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 \
        python-dateutil pytz tzdata six || echo "test dependencies not installed"; rm -rf /tmp/wheels
COPY selector.py map.json.gz tpcds_fp.py tpcds_res.py maxnodes_fp.py /app/
COPY tpcds_data /app/tpcds_data
# in-container runner: the pinned test suite and the test list (to find the tests runnable here)
COPY runner.py tests.json runner_pinned.json tpcds_perq.json /app/
COPY tests_pinned /app/pinned/tests
# TPC-DS stage fingerprints of the pinned commit, computed here in the scoring image itself:
# writes /app/pinned (pinned sqlglot from the map), /app/tpcds_spec.json, /app/tpcds_pinned.json.
# If the two build runs disagree, no fingerprints are written and the check is off.
RUN python /app/selector.py --build-pinned
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]

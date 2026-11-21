# The scorer builds this on linux/amd64 and runs it with no network.  The
# digest is the image the organizers labeled every commit in; the selector
# imports sqlglot (never its tests) under this exact Python.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0

# sqlglot at the pinned commit, from its own repository (the only download).
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot

COPY tools/snapshot.py tools/check_image.py /app/tools/
COPY blast /app/blast
COPY selector.py /app/selector.py
COPY maps /app/maps

# Compile everything once (the run has a read-only disk), fingerprint the
# definition-time values of the pinned tree, and check the result end to end.
RUN python -m compileall -q /opt/sqlglot/sqlglot /app/blast /app/tools /app/selector.py \
 && python -S /app/blast/snapshot_child.py base /opt/sqlglot /opt/blast-base/base.json \
 && chmod -R a+rX /app /opt/sqlglot /opt/blast-base \
 && python /app/tools/check_image.py /opt/sqlglot /app/maps /app/selector.py

CMD ["python", "-S", "/app/selector.py"]

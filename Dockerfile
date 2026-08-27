FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# supabase/dev/ IS included here: this box runs accelerated synthetic tests, and
# `rpt test-lead` needs reset_test_lead_by_name.sql to replace a prior synthetic run.
COPY supabase ./supabase
COPY fixtures ./fixtures
RUN mkdir -p /app/logs
CMD ["uvicorn", "rpt_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]

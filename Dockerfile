# Second Opinion — container image.
#
# The image contains code only. Captured Qloo responses are deliberately not
# copied in: they must not be redistributed, so a deployed instance is meant to
# run in live mode with QLOO_API_KEY supplied by the platform at runtime.
#
# The audit budgets (live: SO_AUDIT_PER_MINUTE, SO_AUDIT_BURST,
# SO_MAX_CONCURRENT_AUDITS; fixture: SO_FIXTURE_AUDIT_PER_MINUTE,
# SO_FIXTURE_AUDIT_BURST, SO_FIXTURE_MAX_CONCURRENT_AUDITS) are plain environment
# configuration and live in process memory, so they are reset by any restart or
# redeploy; nothing here needs changing to set them. Only live audits reach Qloo,
# so a deployed instance cares about the first three.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY second_opinion ./second_opinion
COPY web ./web

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["python3", "-m", "second_opinion.server"]

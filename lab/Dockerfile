FROM python:3.12-slim

WORKDIR /app

# System deps for cryptography and h3
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        && rm -rf /var/lib/apt/lists/*

# Pip dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the LAB
COPY propagation_lab ./propagation_lab
COPY README.md DEMO_SCRIPT.md ./

# Default: run the demo, the verifier, the tamper tests, then the dashboard.
# Override CMD to run any subset.
ENV PORT=5050
EXPOSE 5050

CMD ["sh", "-c", "\
    python -m propagation_lab.demo.run_demo && \
    echo '---' && \
    python -m propagation_lab.verify.run_verify && \
    echo '---' && \
    python -m propagation_lab.verify.tamper_tests && \
    echo '---' && \
    echo 'Dashboard at http://localhost:${PORT}' && \
    python -m propagation_lab.dashboard.app \
"]

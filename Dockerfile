FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY zhub/ ./zhub/

RUN pip install --no-cache-dir '.[server]'

EXPOSE 8080
CMD ["python", "-m", "zhub.server", "--host", "0.0.0.0", "--port", "8080"]

---
name: Bug report
about: Something works incorrectly or crashes
title: "[bug] "
labels: bug
---

**What happened**
A clear, factual description of what went wrong.

**What you expected**
A clear description of what should have happened.

**Reproduction**
Smallest set of commands / code that triggers the bug:

```bash
# e.g.
python -m zhub up --no-tunnel
curl -X POST http://localhost:8080/...
```

**Environment**
- zhub version: <output of `python -c "import zhub; print(zhub.__version__)"`>
- Python: <output of `python --version`>
- OS: <Linux distro / macOS / Windows version>
- Brain in use (if relevant): <Ollama / Groq / OpenAI / ...>
- Deployment: <local / Docker / VPS+systemd / docker-compose>

**Logs**
Relevant lines from `journalctl -u zhub` or stdout. Redact API keys.

**Doctor output**
```
$ python -m zhub doctor
<paste output>
```

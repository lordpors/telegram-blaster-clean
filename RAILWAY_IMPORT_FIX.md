# Railway import fix

Error fixed:

```text
ModuleNotFoundError: No module named 'app'
```

Use this Start Command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
```

The package supports both Railway layouts:

1. Root Directory empty / repository root.
2. Root Directory set to `app`.

Recommended: leave Railway Root Directory empty and upload the ZIP contents so
`main.py`, `app/`, `Procfile`, `requirements.txt`, and `railway.json` are at the
repository root.

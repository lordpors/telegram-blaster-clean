# Railway crash fix

The app now resolves `static`, `templates`, `uploads`, and `blaster.db` from the
actual Python file location instead of relying on Railway's current working
directory.

Deploy the contents of this ZIP at the repository root. The root must contain:

- `Procfile`
- `requirements.txt`
- `runtime.txt`
- `app/`

Railway start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
```

For persistent Telegram sessions/history, attach a Railway Volume and point the
database/session storage to that mounted path. Railway's normal filesystem can
be replaced during redeploys.

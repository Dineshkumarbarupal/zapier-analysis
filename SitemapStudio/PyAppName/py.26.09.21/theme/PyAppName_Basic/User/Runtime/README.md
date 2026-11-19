# Runtime notes

The application uses the Playwright-managed Chromium runtime installed by:

```powershell
python -m playwright install chromium
```

Do not put credentials or user screenshots in this directory. Browser caches and temporary runtime state should remain local and should not be committed.

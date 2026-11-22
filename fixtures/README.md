# Request templates

The request templates deliberately omit a fixed deadline. Set `deadline` to a timezone-aware ISO timestamp (e.g. three weeks from the demo date) before POSTing to `/api/runs`. The dashboard supplies this field automatically. All amounts are INR paise.

Synthetic supplier records live in `backend/app/providers/fixtures/catalog.json` so they are included in the Python package. `.example.invalid` domains and `fixture://` references are identifiers, not live websites.

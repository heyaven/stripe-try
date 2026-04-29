# Google SERP PoC

Small proof of concept for collecting Google SERP HTML, preserving the raw
response, parsing first-page organic links, and comparing parsed links with a
paid SERP JSON response.

## Run

```bash
python3 scripts/google_serp_poc.py \
  --query "阿里巴巴" \
  --hl zh-CN \
  --gl hk \
  --reference-json /home/ubuntu/.cursor/projects/workspace/uploads/response.json
```

Outputs are written under `data/runs/<timestamp>/`:

- `raw/google.html`: original Google HTML for later correction analysis.
- `parsed/google_parsed.json`: parsed organic results and basic SERP signals.
- `reports/reference_comparison.json`: overlap against the paid JSON organic URLs.

If Google returns an intermediate JavaScript retry page in this environment,
the raw HTML is still saved and `serp_state.looks_like_enablejs` is set to
`true` so the run is not mistaken for a parser-completeness failure.
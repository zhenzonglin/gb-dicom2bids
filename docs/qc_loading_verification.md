# QC startup/loading patch verification

## Scope

The legacy private inventory format and all candidate identities are preserved. The loader
streams its JSON array and constructs records directly instead of retaining the complete JSON
text and a second list of dictionaries. No new dependency, inventory migration, source-image
scan, quality decision or BIDS copy is required. Existing manual decisions remain authoritative.

Startup reports progress to stderr, prepares the in-memory identification index before printing
the ready URL, and never throttles based on CPU or disk utilization. Startup/HTTP list loading
has no fixed deadline. A filter change may still cancel a superseded request. Unrelated image
preparation and archival safety checks have not been changed.

## Local verification

- Full tracked regression suite: 170 tests passed (14 existing synthetic DICOM deprecation warnings).
- UTF-8, compact/pretty JSON, 1-byte chunk boundaries, unchanged input bytes and record order.
- Empty, malformed, truncated and changed-during-read inventories; no silently partial success.
- Warmup precedes HTTP service creation; errors/interruption do not print a ready URL.
- Group cache warmup never opens source images or invokes resource gates.
- A disconnected browser does not cause a second write; unrelated I/O errors still propagate.
- Headless browser: virtual 125-second pending identification request stays active, updates the
  elapsed display and sends no repeated auxiliary status queries. Completion, failure/retry and
  superseded-request cancellation pass with no JavaScript errors or external asset requests.
- Live loopback server with synthetic NIfTI: identification propagation, stage transition,
  image display, manual quality save and reload all pass using the existing browser check.
- Ruff, Python compilation, JavaScript syntax checks, source distribution and wheel builds pass.

Repeat the slow-request browser check using the existing optional test tooling:

```bash
python scripts/check_qc_loading_browser.py --output work/loading-browser-check
```

## Synthetic memory measurement

One local run with 24,000 synthetic records in a 25.0 MiB inventory measured Python allocation
peaks of 78.0 MiB (legacy) and 60.7 MiB (streamed). Elapsed times under allocation tracing were
0.69 s and 0.94 s respectively. This verifies lower peak allocation, **not a guaranteed speedup**.
Real network storage latency and workstation startup time remain to be verified on site.

# Public history sanitization rehearsal

Create a private JSON file outside the repository with `{"denylist":["value"]}`. Run:

```powershell
python scripts/audit_and_rehearse_history_sanitization.py `
  --source . `
  --denylist C:\Super1\runtime_trust\history-denylist.json `
  --report outputs\reports\history_sanitization_rehearsal_v4.json
```

The command scans every blob reachable from every local ref/tag, rewrites only a disposable mirror with equal-length redactions, rescans it, and verifies that the source HEAD and origin URL did not change. Output never contains denylisted values. Review the object/path counts before an authorized operator performs rotation and remote history replacement. This rehearsal never pushes or modifies source history.

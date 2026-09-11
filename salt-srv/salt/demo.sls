# Acceptance fixture: a trivial state so dry-run/apply highstate has
# something real to report. Dev only.
/tmp/overstate-demo.txt:
  file.managed:
    - contents: managed by overstate acceptance

# Sanitized Ashby fixtures

These files follow the documented response shape of Ashby's public Job Posting API
(`GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}`, see
https://developers.ashbyhq.com/docs/public-job-posting-api). All content is fabricated:

- the board is `example-board`, and identifiers are synthetic UUIDs;
- the employer, text, and compensation are invented;
- no real posting, person, or company data is included.

`board_v1.json` covers valid, unlisted, malformed, wrong-host/board, identity-conflict,
in-response-duplicate, non-object, HTML-only, ambiguous-compensation, and conflicting
remote-flag records. Tests derive the "changed" and "removed" board states from it
programmatically.

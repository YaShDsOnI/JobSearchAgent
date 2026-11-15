# Sanitized Greenhouse and Lever fixtures

`greenhouse/board_v1.json` follows the documented response shape of Greenhouse's public Job Board API (`GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`, see https://developers.greenhouse.io/job-board.html).

`../lever/site_v1.json` follows Lever's public Postings API (`GET https://api.lever.co/v0/postings/{site}?mode=json`, see https://github.com/lever/postings-api).

All content is fabricated:

- the boards are `exampleco` and `example-site`;
- identifiers are synthetic;
- employers, text, and pay are invented;
- no real posting, person, or company data is included.

The fixtures cover:

- valid, remote, hybrid, and on-site records;
- HTML-escaped content with a script and an external link;
- requirement and "nice to have" sections;
- a careers-domain URL, with and without a reviewed careers host;
- a missing title, an invalid identifier, a wrong host or site, and a hosted URL that disagrees with the ID;
- an in-response duplicate, a non-object element, an incomplete salary range, and an unspecified workplace type.

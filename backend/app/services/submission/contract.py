"""The reviewed form contract for the sandbox ATS adapter.

The field set, selectors, and pause detectors are fixed here, in reviewed code. Page text
never chooses fields, values, URLs, or actions.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    question_key: str  # Package answer key; also the form field `name`.
    kind: str  # "text" | "select"


FORM_SELECTOR = "form#application-form"
SUBMIT_SELECTOR = "#submit-application"
RESUME_SELECTOR = 'input[type="file"][name="resume"]'
CONFIRMATION_SELECTOR = "[data-confirmation-id]"

FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("full_name", "text"),
    FieldSpec("email", "text"),
    FieldSpec("phone", "text"),
    FieldSpec("current_location", "text"),
    FieldSpec("work_authorization", "select"),
    FieldSpec("sponsorship_required", "select"),
    FieldSpec("notice_period", "text"),
    FieldSpec("relocation", "select"),
    FieldSpec("salary_expectation", "text"),
)
KNOWN_FIELD_NAMES = frozenset({spec.question_key for spec in FIELDS} | {"resume"})

CAPTCHA_SELECTORS = (
    ".g-recaptcha", ".h-captcha", "[data-captcha]", 'iframe[src*="captcha"]',
    'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]', 'iframe[src*="turnstile"]',
)
LOGIN_SELECTORS = ('input[type="password"]', 'input[autocomplete="one-time-code"]')
ATTESTATION_SELECTORS = ("[data-attestation]",)

NAVIGATION_TIMEOUT_MS = 15_000
CONFIRMATION_TIMEOUT_MS = 15_000

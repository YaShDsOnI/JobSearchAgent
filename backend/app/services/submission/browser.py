"""Playwright browser runner for the sandbox ATS adapter. The only browser-importing module.

Rules (docs/application-agent.md, "Browser runner rules as implemented"):
- one fresh, ephemeral context per run; no stored state, downloads, or service workers;
- every request is intercepted and only the exact destination origin is allowed;
- CAPTCHA, login/MFA, unknown required fields, attestations, dialogs, and off-site
  navigation pause the run; nothing is bypassed;
- values come only from the package; the field set comes from the reviewed contract;
- no traces, screenshots, or HAR files.
"""

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.services.sources.normalize import stable_hash
from app.services.submission import contract
from app.services.submission.types import (
    AbortBeforeClick,
    FillResult,
    RunRequest,
    SubmitResult,
)


class PauseRun(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Guard:
    origin: str
    offsite: list[str] = field(default_factory=list)
    dialogs: int = 0

    def check(self) -> None:
        if self.offsite:
            raise PauseRun("offsite_navigation")
        if self.dialogs:
            raise PauseRun("dialog")


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class PlaywrightRunner:
    """Real browser runner. Tests may substitute any object with `fill` and `submit`."""

    def __init__(
        self,
        *,
        headless: bool = True,
        confirmation_timeout_ms: int = contract.CONFIRMATION_TIMEOUT_MS,
    ) -> None:
        self._headless = headless
        self._confirmation_timeout_ms = confirmation_timeout_ms

    @contextmanager
    def _page(self, origin: str) -> Iterator[tuple[Page, _Guard]]:
        guard = _Guard(origin=origin.lower())
        with sync_playwright() as playwright:
            browser: Browser = playwright.chromium.launch(headless=self._headless)
            context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
                java_script_enabled=True,
                bypass_csp=False,
                ignore_https_errors=False,
            )
            try:
                def route_handler(route: Route) -> None:
                    request = route.request
                    if _origin_of(request.url) == guard.origin:
                        route.continue_()
                        return
                    if request.is_navigation_request():
                        guard.offsite.append(_origin_of(request.url))
                    route.abort()

                context.route("**/*", route_handler)
                page = context.new_page()
                page.set_default_timeout(contract.NAVIGATION_TIMEOUT_MS)

                def on_dialog(dialog: Any) -> None:
                    guard.dialogs += 1
                    dialog.dismiss()

                page.on("dialog", on_dialog)
                yield page, guard
            finally:
                context.close()
                browser.close()

    def _open_and_check(self, page: Page, guard: _Guard, request: RunRequest) -> None:
        try:
            page.goto(request.url, wait_until="load")
        except PlaywrightError as exc:
            if guard.offsite:
                raise PauseRun("offsite_navigation") from exc
            raise PauseRun("page_unavailable") from exc
        page.wait_for_timeout(250)  # Let load-time scripts (redirects, dialogs) run.
        guard.check()
        if _origin_of(page.url) != guard.origin:
            raise PauseRun("offsite_navigation")
        if any(page.locator(selector).count() for selector in contract.CAPTCHA_SELECTORS):
            raise PauseRun("captcha")
        if any(page.locator(selector).count() for selector in contract.LOGIN_SELECTORS):
            raise PauseRun("mfa_or_login")
        if urlsplit(page.url).path != urlsplit(request.url).path:
            raise PauseRun("form_contract_mismatch")
        form = page.locator(contract.FORM_SELECTOR)
        if form.count() != 1:
            raise PauseRun("form_contract_mismatch")
        if any(form.locator(selector).count() for selector in contract.ATTESTATION_SELECTORS):
            raise PauseRun("attestation_required")
        required = form.locator("input[required], select[required], textarea[required]")
        for index in range(required.count()):
            name = required.nth(index).get_attribute("name") or ""
            if name not in contract.KNOWN_FIELD_NAMES:
                raise PauseRun("unknown_required_field")
        for spec in contract.FIELDS:
            if form.locator(f'[name="{spec.question_key}"]').count() != 1:
                raise PauseRun("form_contract_mismatch")
        if form.locator(contract.RESUME_SELECTOR).count() != 1:
            raise PauseRun("form_contract_mismatch")
        if form.locator(contract.SUBMIT_SELECTOR).count() != 1:
            raise PauseRun("form_contract_mismatch")

    def _fill(self, page: Page, guard: _Guard, request: RunRequest) -> dict[str, Any]:
        form = page.locator(contract.FORM_SELECTOR)
        fields: list[dict[str, Any]] = []
        for spec in contract.FIELDS:
            element = form.locator(f'[name="{spec.question_key}"]')
            element_id = element.get_attribute("id")
            label = ""
            if element_id:
                labels = page.locator(f'label[for="{element_id}"]')
                label = labels.first.inner_text().strip() if labels.count() else ""
            answer = request.answers.get(spec.question_key)
            value = str(answer["value"]) if answer is not None else ""
            if spec.kind == "select":
                if value:
                    element.select_option(value=value)
                shown = element.evaluate(
                    "e => e.selectedIndex >= 0 && e.value ? e.options[e.selectedIndex].text : ''"
                )
                read_back = element.input_value()
            else:
                if value:
                    element.fill(value)
                read_back = element.input_value()
                shown = read_back
            if read_back != value:
                raise PauseRun("form_contract_mismatch")
            fields.append({
                "question_key": spec.question_key,
                "label": label[:200],
                "display_value": str(shown)[:500],
                "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "provenance": answer.get("provenance") if answer else None,
                "sent": bool(value),
            })
        actual = hashlib.sha256(request.resume_path.read_bytes()).hexdigest()
        if actual != request.resume_sha256:
            raise PauseRun("cv_hash_mismatch")
        form.locator(contract.RESUME_SELECTOR).set_input_files(str(request.resume_path))
        page.wait_for_timeout(100)
        guard.check()
        return {
            "fields": fields,
            "resume": {"filename": request.resume_path.name, "sha256": actual},
            "destination_url": request.url,
        }

    def fill(self, request: RunRequest) -> FillResult:
        """Open, check, fill, and read back the form. Never clicks submit."""
        try:
            with self._page(request.origin) as (page, guard):
                self._open_and_check(page, guard, request)
                manifest = self._fill(page, guard, request)
                return FillResult(manifest=manifest, fill_hash=stable_hash(manifest))
        except PauseRun as pause:
            return FillResult(pause_reason=pause.reason)

    def submit(
        self,
        request: RunRequest,
        *,
        expected_fill_hash: str,
        before_click: Callable[[], None],
    ) -> SubmitResult:
        """Refill, require the confirmed manifest, run the before-click hook, click once."""
        clicked = False
        try:
            with self._page(request.origin) as (page, guard):
                self._open_and_check(page, guard, request)
                manifest = self._fill(page, guard, request)
                if stable_hash(manifest) != expected_fill_hash:
                    return SubmitResult(outcome="paused", reason="form_changed", manifest=manifest)
                guard.check()
                try:
                    before_click()  # Re-checks kill switches; commits submit_clicked_at.
                except AbortBeforeClick as abort:
                    return SubmitResult(outcome="aborted", reason=abort.reason)
                clicked = True
                try:
                    page.locator(contract.SUBMIT_SELECTOR).click()
                    marker = page.locator(contract.CONFIRMATION_SELECTOR)
                    marker.wait_for(state="attached", timeout=self._confirmation_timeout_ms)
                    if _origin_of(page.url) != guard.origin:
                        return SubmitResult(outcome="unverified", reason="offsite_after_submit",
                                            clicked=True, manifest=manifest)
                    confirmation = (marker.first.get_attribute("data-confirmation-id") or "")
                    confirmation = confirmation.strip()[:128]
                    if not confirmation:
                        return SubmitResult(outcome="unverified", reason="no_confirmation",
                                            clicked=True, manifest=manifest)
                    return SubmitResult(outcome="verified", confirmation_id=confirmation,
                                        clicked=True, manifest=manifest)
                except PlaywrightError:
                    return SubmitResult(outcome="unverified", reason="no_confirmation",
                                        clicked=True, manifest=manifest)
        except PauseRun as pause:
            if clicked:
                return SubmitResult(outcome="unverified", reason=pause.reason, clicked=True)
            return SubmitResult(outcome="paused", reason=pause.reason)
        except PlaywrightError:
            if clicked:
                return SubmitResult(outcome="unverified", reason="browser_error", clicked=True)
            return SubmitResult(outcome="paused", reason="page_unavailable")

"""Aggregator interface + shared payment dataclasses.

Every payment aggregator (DigiKUNTZ today, others later) implements the
`Aggregator` ABC. The generic browser-IA engine (core/browser_runner.py) and the
server (core/server.py) talk to aggregators only through this interface, so
adding an aggregator means adding one folder under aggregators/ — no core change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.browser import BrowserController, CapturedRequest
    from core.llm_client import LlmClient


@dataclass
class PaymentRequest:
    amount: int  # XAF
    phone: str
    network: str  # MTN or Orange
    email: str
    sender_name: str = "Rauvalia"
    callback_url: str = ""  # defaults to aggregator's callback if left empty


@dataclass
class PaymentResult:
    success: bool = False
    error: str = ""
    transaction_id: str = ""
    payment_status: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Captured charge request (the deduced curl replay)
    flutterwave_charge_url: str = ""
    flutterwave_charge_body: str = ""
    flutterwave_charge_response: str = ""
    curl_replay: str = ""
    # Plaintext data BEFORE encryption
    plaintext_payload: str = ""
    public_key: str = ""
    # Verify endpoint for curl replay polling
    verify_url: str = ""
    verify_request_body: str = ""
    verify_last_response: str = ""
    verify_curl: str = ""
    # USSD validation result after waiting
    final_status: str = ""
    final_message: str = ""
    # Console + network error signals captured around the Pay click
    error_signals: dict = None
    # All captured network requests summary
    captured_requests: list[dict] = None
    # Per-turn AI reasoning trace (browser mode) — what the agent saw/thought/did.
    trace: list[dict] = None

    def __post_init__(self):
        if self.captured_requests is None:
            self.captured_requests = []
        if self.error_signals is None:
            self.error_signals = {}
        if self.trace is None:
            self.trace = []


@dataclass
class CurlTemplate:
    """Reusable per-aggregator replay template, persisted in the DB.

    Deduced by the browser mode (from the captured /charge + /verify requests and
    the crypto-hook public key) and reloaded by the replay mode.
    """
    charge_url: str = ""
    verify_url: str = ""
    init_url: str = ""
    upgrade_url: str = ""
    hosted_pay_url: str = ""
    headers: dict = field(default_factory=dict)
    payload_skeleton: dict = field(default_factory=dict)
    public_key_rsa: str = ""
    flw_pub_key: str = ""


class Aggregator(ABC):
    """Interface every aggregator module implements.

    Grounded in what the DigiKUNTZ browser_flow + replay_flow already do.
    """

    name: str = "base"

    # Canonical networks this aggregator accepts (the exact values the backend
    # expects). Each aggregator overrides this. Validation in /pay rejects
    # anything else and echoes this list back.
    supported_networks: list[str] = []

    def __init__(self, browser: "BrowserController", llm: "LlmClient", db=None, config=None):
        self.browser = browser
        self.llm = llm
        self.db = db
        self.config = config

    def normalize_network(self, network: str) -> str | None:
        """Return the canonical network value for `network`, or None if invalid.

        Matches case-insensitively against supported_networks and common
        substrings (e.g. 'orange' -> 'Orangemoney') so callers can be lenient,
        while the canonical value is what gets sent downstream.
        """
        if not network:
            return None
        n = network.strip().lower()
        for canonical in self.supported_networks:
            c = canonical.lower()
            if n == c or n in c or c.split()[0] in n:
                return canonical
        return None

    # --- transaction creation (from _create_transaction / replay step1) ---
    @abstractmethod
    async def create_transaction(self, req: PaymentRequest) -> dict:
        """Create a transaction; return at least {transactionRef, paymentLink}."""

    # --- browser-IA hooks consumed by the generic engine ---
    @abstractmethod
    def browser_objective(self, req: PaymentRequest) -> str:
        """The natural-language objective handed to the reasoning loop."""

    @abstractmethod
    async def decide_browser_outcome(self, req: PaymentRequest, loop_result, result: PaymentResult) -> None:
        """Decide the final outcome after the reasoning loop (in place on result).

        Provider-specific (e.g. DigiKUNTZ's USSD watch loop + classifier/LLM
        verdict); the generic runner delegates the verdict here.
        """

    @abstractmethod
    def network_label(self, network: str) -> str:
        """Human-facing network name (e.g. 'Orange Money')."""

    @abstractmethod
    def charge_request_matcher(self, r: "CapturedRequest") -> bool:
        """Predicate selecting the charge request among captured requests."""

    @abstractmethod
    def verify_request_matcher(self, r: "CapturedRequest") -> bool:
        """Predicate selecting verify/polling requests among captured requests."""

    @abstractmethod
    def checkout_url_predicate(self, url: str) -> bool:
        """True while the URL is still on the provider's checkout (not redirected)."""

    # --- template extraction (browser mode -> DB) ---
    @abstractmethod
    def extract_curl_template(
        self, charge: "CapturedRequest", verify: "CapturedRequest | None", public_key: str
    ) -> CurlTemplate | None:
        """Build a reusable CurlTemplate from the captured browser flow."""

    # --- replay mode (from replay_flow steps) ---
    @abstractmethod
    async def replay(self, req: PaymentRequest, template: CurlTemplate) -> PaymentResult:
        """Reproduce the payment without a browser, using a stored template."""

    # --- browser mode (full AI-driven flow) ---
    @abstractmethod
    async def pay_via_browser(self, req: PaymentRequest) -> PaymentResult:
        """Run the full browser-IA flow and return the result."""

    # --- result interpretation (interpret_charge/verify/ping + _interpret) ---
    @abstractmethod
    def interpret_status(self, stage: str, resp: dict, network: str):
        """Map a raw provider response at a given stage to (status, message)|None."""

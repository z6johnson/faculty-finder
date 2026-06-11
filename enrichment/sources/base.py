"""Base class for enrichment data sources."""

import logging
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


class BaseSource(ABC):
    """Abstract base class for all enrichment sources.

    Subclasses implement fetch() and return raw data dicts.
    Rate limiting and error handling are provided by this base.
    """

    # Override in subclasses
    source_name = "base"
    min_request_interval = 1.0  # seconds between requests
    confidence = 0.5

    def __init__(self):
        self._last_request_time = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "UCSD-GrantMatch/1.0 (academic research tool; "
                          "contact: hwsph-grants@ucsd.edu)",
        })

    def _rate_limit(self):
        """Enforce minimum interval between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self._last_request_time = time.time()

    # Throttling (429) and transient unavailability (503) get retried with
    # backoff; anything else keeps the warn-and-return-None contract.
    _RETRY_STATUSES = (429, 503)
    _MAX_ATTEMPTS = 3
    _MAX_BACKOFF = 30.0
    _MAX_INTERVAL = 1.0  # ceiling for adaptive politeness after a 429

    def _request(self, method, url, **kwargs):
        """Rate-limited request with error handling and 429/503 backoff."""
        for attempt in range(self._MAX_ATTEMPTS):
            self._rate_limit()
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                status = getattr(getattr(e, "response", None),
                                 "status_code", None)
                if (status in self._RETRY_STATUSES
                        and attempt < self._MAX_ATTEMPTS - 1):
                    retry_after = e.response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 0.0
                    delay = min(max(delay, 2.0 ** (attempt + 1)),
                                self._MAX_BACKOFF)
                    if status == 429:
                        # Server says we're too fast: stay slower for the
                        # rest of this run, not just this request.
                        self.min_request_interval = min(
                            self.min_request_interval * 2, self._MAX_INTERVAL)
                    logger.info("%s %s got %s — retrying in %.1fs "
                                "(attempt %d/%d)", method, url, status, delay,
                                attempt + 1, self._MAX_ATTEMPTS)
                    time.sleep(delay)
                    continue
                logger.warning("%s request to %s failed: %s", method, url, e)
                return None
        return None

    def _get(self, url, **kwargs):
        """Rate-limited GET request with error handling."""
        return self._request("GET", url, **kwargs)

    def _post(self, url, **kwargs):
        """Rate-limited POST request with error handling."""
        return self._request("POST", url, **kwargs)

    @abstractmethod
    def fetch(self, faculty_dict):
        """Fetch enrichment data for a faculty member.

        Args:
            faculty_dict: Dict with at least first_name, last_name, email.

        Returns:
            Dict of extracted data, or None if no data found.
            Keys should map to Faculty model fields.
        """

    @abstractmethod
    def fields_provided(self):
        """Return list of Faculty field names this source can populate."""

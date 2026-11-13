"""Phone numbers as identity keys (not as channel addresses).

A channel writes to the address it was given (``sessions.contact``: WhatsApp's
``+5215512345678``). Identity compares *canonical* numbers, so the same line
is recognised whatever channel, provider or recruiter wrote it down:

* ``whatsapp:`` / ``tel:`` prefixes and separators go; ``00`` is ``+``;
* national numbers are read in ``DEFAULT_PHONE_REGION`` (Spain: 9 digits,
  6/7/8/9 -> ``+34``); anything else without a country code is ambiguous;
* WhatsApp quirks: Mexican mobiles keep a legacy ``1`` after ``+52``;
* withheld caller IDs (``anonymous``, Twilio's placeholder numbers...) are
  nobody's identity.

Only the markets we recruit in (ES, MX) have rules: swap in ``phonenumbers``
(libphonenumber) behind this module when that changes.
"""

from __future__ import annotations

import re
from typing import Optional

from ..config import settings

# What carriers and providers put in the caller ID when the number is withheld.
WITHHELD_WORDS = frozenset({
    "anonymous", "restricted", "unknown", "private", "unavailable", "blocked", "withheld",
    "anonimo", "anónimo", "privado", "desconocido", "oculto", "no disponible",
})
# Twilio's placeholders: ANONYMOUS, RESTRICTED, BLOCKED, UNAVAILABLE, UNKNOWN.
WITHHELD_NUMBERS = frozenset({"+266696687", "+7378742833", "+2562533", "+8656696", "+86282452253"})

_NATIONAL = {  # region -> (country code, national digits, allowed first digits)
    "ES": ("34", 9, "6789"),
    "MX": ("52", 10, "123456789"),
}
_COUNTRY_CODES = tuple(code for code, _, _ in _NATIONAL.values())
_E164 = re.compile(r"\+\d{8,15}")


def is_withheld(raw: Optional[str]) -> bool:
    value = (raw or "").strip().lower().removeprefix("tel:")
    return value in WITHHELD_WORDS or value.replace(" ", "") in WITHHELD_NUMBERS


def canonical_phone(raw: Optional[str], default_region: Optional[str] = None) -> Optional[str]:
    """The identity key of a phone number (``+34600111222``), or None when it identifies nobody."""
    value = (raw or "").strip()
    for prefix in ("whatsapp:", "tel:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):].strip()
    if not value or is_withheld(value) or re.search(r"[a-zA-Z]", value):  # words, client:..., sip:...
        return None
    digits = re.sub(r"\D", "", value)
    international = value.startswith("+") or value.startswith("(+")
    if not international and digits.startswith("00"):
        digits, international = digits[2:], True
    if not international:
        code, length, first = _NATIONAL.get((default_region or settings.default_phone_region or "").upper(),
                                            ("", 0, ""))
        if code and len(digits) == length and digits[:1] in first:
            digits = code + digits
        elif len(digits) >= 11 and digits.startswith(_COUNTRY_CODES):  # international, "+" left out
            pass
        else:
            return None  # ambiguous: ask for the country code
    number = "+" + digits
    if number in WITHHELD_NUMBERS:
        return None
    mexican_mobile = re.fullmatch(r"\+521(\d{10})", number)
    if mexican_mobile:
        number = "+52" + mexican_mobile.group(1)
    return number if _E164.fullmatch(number) else None

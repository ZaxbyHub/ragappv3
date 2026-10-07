"""Password-rule frontend/backend contract (issue #776, UI-ENH-12 / UI-R4-13).

The shared PasswordRequirements component is the single frontend statement of
the password rules. This test pins that its wording states exactly the three
rules ``password_strength_check`` enforces in
``backend/app/services/auth_service.py`` (>= 8 characters, >= 1 digit,
>= 1 uppercase letter) so the two surfaces cannot silently diverge.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "frontend" / "src" / "components" / "shared" / "PasswordRequirements.tsx"
AUTH_SERVICE = ROOT / "backend" / "app" / "services" / "auth_service.py"


def test_frontend_component_states_the_three_server_rules() -> None:
    source = COMPONENT.read_text(encoding="utf-8")
    # The static sentence + the three live-mode rule labels.
    assert re.search(r"At least 8 characters, one digit, and one uppercase letter", source)
    assert re.search(r'At least 8 characters"', source)
    assert re.search(r'At least one digit"', source)
    assert re.search(r'At least one uppercase letter"', source)


def test_backend_still_enforces_the_same_three_rules() -> None:
    source = AUTH_SERVICE.read_text(encoding="utf-8")
    assert re.search(r"len\(plain_password\) < 8", source)
    assert re.search(r"any\(char\.isdigit\(\) for char in plain_password\)", source)
    assert re.search(r"any\(char\.isupper\(\) for char in plain_password\)", source)

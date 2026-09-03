from __future__ import annotations

import sys
from pathlib import Path


PAYPAL_PROTOCOL_ROOT = Path(__file__).parent / "backend" / "paypal_agreement_protocol"
if str(PAYPAL_PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PAYPAL_PROTOCOL_ROOT))

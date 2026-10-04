"""jhmesh.messages.describe: the text of every opcode it names, pinned byte for byte in `describe_golden.txt`.

Each named opcode (every SIG opcode whose text is not the `SIG op` fallback, every vendor opcode under the JUNG
company id, one under another) is described with a fixed set of parameter lengths, under an application key and,
where the text differs, under a device key. After a deliberate change of the text, regenerate the file with
`python tests/jhmesh/test_describe_golden.py` and review the diff.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

from jhmesh import messages as M
from jhmesh.pdu import encode_opcode
from jhmesh.vendor_models import JUNG_CID

GOLDEN = Path(__file__).with_name("describe_golden.txt")
OTHER_CID = 0x0059  # a company that is not JUNG: its vendor messages are named by company and opcode only


def _payloads() -> Iterator[bytes]:
    for n in (*range(13), 16, 19, 24):
        yield bytes((i * 37 + 11) & 0xFF for i in range(n))
    yield bytes(2)
    yield bytes(10)


def _opcodes() -> Iterator[bytes]:
    probe = bytes(range(16))
    for op in (*range(0x7F), *range(0x8000, 0xC000)):
        opcode = encode_opcode(op)
        if not M.describe(opcode + probe).startswith(f"SIG op {op:04X} "):
            yield opcode
    for op in range(0x40):
        yield encode_opcode(op, JUNG_CID)
    yield encode_opcode(0x01, OTHER_CID)


def lines() -> list[str]:
    out = []
    for opcode in _opcodes():
        for params in _payloads():
            pdu = opcode + params
            text = M.describe(pdu)
            out.append(f"- {pdu.hex()} {text}")
            keyed = M.describe(pdu, devkey=True)
            if keyed != text:
                out.append(f"K {pdu.hex()} {keyed}")
    return out


def test_describe_matches_the_golden_text() -> None:
    assert lines() == GOLDEN.read_text(encoding="utf-8").splitlines()


if __name__ == "__main__":  # pragma: no cover
    GOLDEN.write_text("\n".join(lines()) + "\n", encoding="utf-8")
    sys.exit(0)

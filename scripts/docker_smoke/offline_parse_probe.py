"""Offline first-parse probe (issue #1008 CI smoke).

Runs INSIDE the built backend image with no network:

    docker run --rm --network none --entrypoint python <image> \
        /smoke/parse_probe.py /smoke/fixture.pdf

Prints ``OFFLINE_PARSE_OK elements=N`` (N > 0) and exits 0 when the first
document parse returns elements offline — proving the baked NLTK data
(/usr/local/nltk_data via NLTK_DATA) actually serves unstructured's
partitioners in a fresh container. On zero elements or any exception it
prints the failure and exits 1.

The image WORKDIR is /app and backend/app is copied to /app/app, so
``from app.services.document_processor import DocumentParser`` resolves;
if the script is invoked from elsewhere, the fixture directory's parent
is added to sys.path as a fallback.
"""

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("OFFLINE_PARSE_FAIL usage: parse_probe.py <pdf-path>")
        return 1
    target = Path(sys.argv[1])
    # The image's WORKDIR is /app and the backend package lives at /app/app,
    # but running a script by absolute path puts the SCRIPT's directory (not
    # the cwd) on sys.path — put both the image WORKDIR and the fixture's
    # parent there so `app.services...` resolves inside the container.
    for root in ("/app", str(target.parent)):
        if root and root not in sys.path:
            sys.path.insert(0, root)

    try:
        from app.services.document_processor import DocumentParser

        elements = DocumentParser().parse(str(target))
    except Exception as exc:  # noqa: BLE001 — the probe reports, never raises
        print(f"OFFLINE_PARSE_FAIL exception={type(exc).__name__}: {exc}")
        return 1

    count = len(elements)
    if count <= 0:
        print(
            "OFFLINE_PARSE_FAIL zero elements — the NLTK tokenizer-data bake "
            "is missing or not resolving (check NLTK_DATA in the image)"
        )
        return 1
    print(f"OFFLINE_PARSE_OK elements={count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

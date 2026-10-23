"""Segmentation regression suite.

Runs every fixture PDF through extraction + cleaning + segmentation (no AI
calls), batching pages exactly like the app does (10 pages per request,
"Extract More" resuming at the restart page or batch end), and compares the
question numbers found against the hand-verified list in expected.json.

The fixture PDFs are third-party exam papers and are not committed (see
.gitignore); expected.json lists the files the suite expects in fixtures/.

Usage (from backend/):
    venv/bin/python tests/run_segmentation.py            # check all
    venv/bin/python tests/run_segmentation.py --show     # also print found lists
    venv/bin/python tests/run_segmentation.py mole       # only fixtures matching "mole"
"""
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
logging.disable(logging.CRITICAL)

import fitz  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
# Segmentation never calls the API; a placeholder lets this run without a key.
os.environ.setdefault("OPENAI_API_KEY", "not-needed-for-segmentation")
from pdf_processor import PDFProcessor  # noqa: E402

PAGE_BATCH_SIZE = 10  # keep in step with app.py
FIXTURES = os.path.join(HERE, "fixtures")


def segment_all_batches(pp: PDFProcessor, path: str) -> list:
    total = fitz.open(path).page_count
    found, start, state = [], 0, None
    while start < total:
        batch = pp._extract_batch(path, start, PAGE_BATCH_SIZE, state)
        found.extend(c["question_number"] for c in batch["chunks"])
        state = batch["marker_state"]
        if batch["end_page"] <= start:  # no progress — would loop forever in the app too
            found.append(f"<STUCK at page {start}>")
            break
        start = batch["end_page"]
    return found


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = "--show" in sys.argv
    with open(os.path.join(HERE, "expected.json")) as f:
        expected = json.load(f)
    tolerated = expected.pop("_tolerated_misses", {})

    pp = PDFProcessor()
    names = sorted(n for n in os.listdir(FIXTURES) if n.endswith(".pdf"))
    if args:
        names = [n for n in names if any(a in n for a in args)]

    passed = failed = unverified = 0
    total_want = total_hit = total_extra = 0
    for name in names:
        found = segment_all_batches(pp, os.path.join(FIXTURES, name))
        want = expected.get(name)
        if want is not None:
            remaining = list(found)
            hit = 0
            for q in want:
                if q in remaining:
                    remaining.remove(q)
                    hit += 1
            total_want += len(want)
            total_hit += hit
            total_extra += len(remaining)
        if want is None:
            unverified += 1
            print(f"?    {name}: {len(found)} found (no expected list yet)")
            show_list = True
        elif found == want or found == [q for q in want if q not in tolerated.get(name, [])]:
            passed += 1
            note = f" (tolerated OCR misses: {tolerated[name]})" if found != want else ""
            print(f"PASS {name}: {len(found)}{note}")
            show_list = show
        else:
            failed += 1
            missing = [q for q in want if q not in found]
            extra = [q for q in found if q not in want]
            print(f"FAIL {name}: found {len(found)}, expected {len(want)}")
            if missing:
                print(f"       missing: {missing}")
            if extra:
                print(f"       extra:   {extra}")
            if not missing and not extra:
                print("       (same items, different order/duplicates)")
            show_list = True
        if show_list:
            print(f"       found: {found}")

    print(f"\n{passed} passed, {failed} failed, {unverified} unverified")
    if total_want:
        print(
            f"questions found: {total_hit}/{total_want} ({100 * total_hit / total_want:.0f}%), "
            f"spurious extras: {total_extra}"
        )
    return 1 if failed or unverified else 0


if __name__ == "__main__":
    sys.exit(main())

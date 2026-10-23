import re
import fitz  # PyMuPDF for better PDF text extraction
from typing import List, Dict, Optional, Union, Tuple
from pydantic import BaseModel, ValidationError, Field
import json
import os
import time
import io
import logging
import concurrent.futures
from openai import OpenAI, RateLimitError
import uuid
import pytesseract
from PIL import Image

logging.basicConfig(
    level=logging.DEBUG,  # <- this is the fix
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("pdf_processor.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 5
MAX_RETRIES = 3

DASHBOARD_DATA_DIR = os.path.expanduser(
    os.getenv("DASHBOARD_DATA_DIR", "~/Desktop/dashboard/public/data")
)


def _load_json_list(filename: str, key: str) -> List[dict]:
    """Load a {'data': {key: [...]}} reference file from the dashboard project."""
    path = os.path.join(DASHBOARD_DATA_DIR, filename)
    if not os.path.exists(path):
        logger.warning(f"Reference data not found at {path}")
        return []
    try:
        with open(path) as f:
            raw = json.load(f)
        items = raw.get("data", {}).get(key, [])
        logger.info(f"Loaded {len(items)} {key} from {path}")
        return items
    except Exception as e:
        logger.warning(f"Failed to load {filename}: {e}")
        return []


ALL_EXAMBOARDS = _load_json_list("examboards.json", "examboards")
ALL_SUBJECTS = _load_json_list("subjects.json", "subjects")
ALL_BUBBLES = _load_json_list("bubbles.json", "bubbles")


def filter_bubbles(
    subject_id: Optional[str] = None, board_id: Optional[str] = None, topic_id: Optional[str] = None
) -> List[dict]:
    """Narrow the full bubble list to the chosen subject/exam board/topic, via
    each bubble's embedded topic_id.subject_id / topic_id.board_id / topic_id._id.
    `topic_id` is optional on top of subject/board — picking a topic is meant
    to further narrow an already-large bubble list, not be required on its own."""
    def matches(b: dict) -> bool:
        topic = b.get("topic_id") or {}
        if subject_id and topic.get("subject_id") != subject_id:
            return False
        if board_id and topic.get("board_id") != board_id:
            return False
        if topic_id and topic.get("_id") != topic_id:
            return False
        return True

    return [b for b in ALL_BUBBLES if matches(b)]


def build_bubble_lookup(
    subject_id: Optional[str] = None, board_id: Optional[str] = None, topic_id: Optional[str] = None
) -> Dict[str, dict]:
    """Build {'<name> :: <topic name>': full bubble object} for the AI to pick from."""
    return {
        f"{b['name']} :: {b.get('topic_id', {}).get('name', '')}": b
        for b in filter_bubbles(subject_id, board_id, topic_id)
        if b.get("name")
    }


class Question(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    question: str
    options: List[str] = []
    answer: Union[str, List[str]] = ""
    type: str = "Multiple Choice"
    multiSelect: bool = False  # True when a Multiple Choice question requires selecting 2+ correct options
    image: List[str] = []  # List of image paths associated with the question
    bubble_id: Optional[Dict] = None  # AI-suggested bubble, resolved against bubbles.json

class PDFProcessor:
    def __init__(self):
        logger.info("Initializing PDFProcessor...")
        try:
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            logger.info("OpenAI client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}")
            raise

    def process_pdf(
        self, filepath: str, subject_id: Optional[str] = None, board_id: Optional[str] = None,
        topic_id: Optional[str] = None, start_page: int = 0, page_count: int = 10,
        marker_state: Optional[list] = None,
    ) -> Dict:
        """`marker_state` is the previous batch's returned "marker_state"
        when this call continues the same document ("Extract More"), so
        question numbering carries on from where it stopped."""
        logger.info(
            f"Starting PDF processing for: {filepath} (subject={subject_id}, board={board_id}, "
            f"topic={topic_id}, pages {start_page}-{start_page + page_count - 1})"
        )
        try:
            batch = self._extract_batch(filepath, start_page, page_count, marker_state)
            question_chunks = batch["chunks"]
            total_pages = batch["total_pages"]
            actual_end_page = batch["end_page"]
            page_image_map = batch["page_image_map"]
            page_question_y = batch["page_question_y"]
            marker_state = batch["marker_state"]

            bubble_lookup = build_bubble_lookup(subject_id, board_id, topic_id)
            if not bubble_lookup and topic_id:
                logger.warning("No bubbles matched the chosen topic; falling back to subject/board only.")
                bubble_lookup = build_bubble_lookup(subject_id, board_id)
            if not bubble_lookup and (subject_id or board_id):
                logger.warning("No bubbles matched the chosen subject/board; falling back to the full bubble list.")
                bubble_lookup = build_bubble_lookup()

            logger.info("Step 4: Processing questions with AI...")
            questions = self._structure_with_ai(question_chunks, page_image_map, page_question_y, bubble_lookup)
            logger.info(f"✓ Processed {len(questions)} questions")

            logger.info("Step 5: Validating question data...")
            validated_questions = self._validate_questions(questions)
            logger.info(f"✓ Validated {len(validated_questions)} questions")

            result = {
                "questions": validated_questions,
                "metadata": {
                    "total_pages": total_pages,
                    "total_questions": len(validated_questions),
                    "start_page": start_page,
                    "end_page": actual_end_page,
                    "has_more": actual_end_page < total_pages,
                },
                "marker_state": marker_state,
            }

            logger.info("✅ PDF processing completed successfully!")
            return result

        except Exception as e:
            logger.error(f"❌ PDF processing failed: {str(e)}")
            raise Exception(f"PDF processing failed: {str(e)}")

    def _extract_batch(
        self, filepath: str, start_page: int, page_count: int, marker_state: Optional[list] = None,
    ) -> Dict:
        """Extract, clean and segment one batch of pages — everything up to
        the AI step, shared by process_pdf and the segmentation test suite.

        The batch can end earlier than start_page + page_count, and the next
        "Extract More" batch resumes from the returned "end_page":
        - at a numbering restart (a new "Medium questions" section back at
          "1"), so the next batch starts that section fresh;
        - otherwise, at the page where this batch's last question starts,
          since that question may run on into pages this batch didn't read —
          the next batch extracts it whole instead of picking up its second
          half as a stray fragment (see _defer_unfinished_question)."""
        logger.info("Step 1: Extracting text from PDF...")
        # Where the numbering stood before this batch (extraction updates
        # marker_state in place as it reads).
        last_num_before = marker_state[1] if marker_state else None
        padded_before = bool(marker_state[3]) if marker_state and len(marker_state) > 3 else False
        raw_text, total_pages, page_image_map, page_question_y, marker_state = self._extract_text(
            filepath, start_page, start_page + page_count, path_state=marker_state
        )
        end_page = min(start_page + page_count, total_pages)
        logger.info(f"✓ Text extracted successfully. Pages: {total_pages}, Characters: {len(raw_text)}")

        logger.info("Step 2: Cleaning extracted text...")
        cleaned_text = self._clean_text(raw_text)

        logger.info("Step 3: Segmenting into question chunks...")
        chunks, restart_page, padded = self._segment_questions(
            cleaned_text, last_num=last_num_before, padded=padded_before
        )
        if len(marker_state) < 4:
            marker_state.append(False)
        marker_state[3] = padded
        logger.info(f"✓ Found {len(chunks)} question chunks")
        if not chunks and len(re.findall(r"[A-Za-z]{3,}", cleaned_text)) >= self._AI_SEGMENT_MIN_WORDS:
            logger.warning("Numbering rules found no questions in a text-heavy batch; asking the AI for question boundaries")
            chunks = self._ai_segment(cleaned_text)
        if restart_page is not None:
            end_page = restart_page
            # The next batch starts a fresh section at "1" — keep only where
            # question numbers sit on the page, not the count.
            marker_state = [[], None] + marker_state[2:]
            logger.info(f"Numbering restart detected at page {restart_page}; capping batch there")
        elif end_page < total_pages:
            chunks, end_page, marker_state = self._defer_unfinished_question(
                chunks, start_page, end_page, marker_state
            )

        return {
            "chunks": chunks,
            "total_pages": total_pages,
            "end_page": end_page,
            "page_image_map": page_image_map,
            "page_question_y": page_question_y,
            "marker_state": marker_state,
        }

    def _defer_unfinished_question(
        self, chunks: List[Dict], start_page: int, end_page: int, marker_state: list,
    ) -> Tuple[List[Dict], int, list]:
        """Drop this batch's last top-level question (all its parts) and end
        the batch at the page it starts on, so the next batch reads it in
        full. A no-op when that question starts on the batch's first page —
        a single question longer than a whole batch has nowhere better to
        split."""
        if not chunks:
            return chunks, end_page, marker_state

        def top(chunk):
            return chunk["question_number"].split(".")[0]

        last = top(chunks[-1])
        last_start = min((c.get("page_nums") or [c["page_num"]])[0] for c in chunks if top(c) == last)
        if last_start <= start_page:
            return chunks, end_page, marker_state

        kept = [c for c in chunks if top(c) != last]
        # The next batch must accept `last` again as the next number in sequence.
        marker_state = [[], int(last) - 1 if last.isdigit() else None] + marker_state[2:]
        logger.info(f"Deferring question {last} (starts on page {last_start}) to the next batch")
        return kept, last_start, marker_state

    # Real words a batch must hold before finding no questions at all counts
    # as the numbering rules failing (rather than a cover page or blank pages).
    _AI_SEGMENT_MIN_WORDS = 150

    def _ai_segment(self, text: str) -> List[Dict]:
        """Fallback for a layout the numbering rules don't understand: ask the
        AI where each question and sub-question starts, then chunk the text
        at those lines exactly as _segment_bare_numbered_questions would
        (a stem's text shared with every part beneath it). Only used when
        the rules found nothing in a batch with plenty of text; any failure
        just leaves the batch with no questions, as before."""
        # Put back the "(c)"/"(i)" labels that extraction swapped for sentinels.
        text = re.sub(r"<<<(?:SUBLETTER|SUBROMAN)_([a-z]+)>>>", r"(\1) ", text)
        lines = re.sub(r"<<<QSTART_[\w-]+>>>", "", text).split("\n")
        line_page: Dict[int, int] = {}
        numbered = []
        page = 0
        for i, line in enumerate(lines):
            m = re.fullmatch(r"\s*<<<PAGE_(\d+)>>>\s*", line)
            if m:
                page = int(m.group(1))
            elif line.strip():
                line_page[i] = page
                numbered.append(f"{i}: {line}")

        prompt = (
            "Below is text extracted from exam paper pages, one line per row, each prefixed "
            "with its line number.\n\n"
            "List every line where a new question or sub-question starts: a question number "
            "(e.g. \"1\", \"12\", \"01\"), a lettered part (\"(a)\") or a roman part (\"(i)\"), "
            "including one that shares a line with others (\"1 (c) (i) Name...\" is a single "
            "start). Ignore numbers that are content: numbered statements or steps inside a "
            "question, table or graph values, page numbers, mark allocations, answer options.\n\n"
            "Return JSON {\"starts\": [{\"line\": <line number>, \"label\": \"<path>\"}]} where "
            "path is the full dotted position: \"3\" for a question with no parts, \"1.c.i\" for "
            "question 1 part (c)(i), \"02.3\" for zero-padded numbering. Use the paper's own "
            "numbers; list starts in order.\n\n" + "\n".join(numbered)
        )
        try:
            response = self._call_openai_with_retry(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            starts = json.loads(response.choices[0].message.content).get("starts", [])
        except Exception as e:
            logger.warning(f"AI segmentation fallback failed: {e}")
            return []

        valid = []
        for s in starts:
            line, label = s.get("line"), str(s.get("label", "")).strip().lower()
            if isinstance(line, int) and line in line_page and re.fullmatch(r"\d{1,2}(?:\.[a-z0-9]{1,4}){0,2}", label):
                valid.append((line, label))
        valid.sort()
        if not valid:
            return []

        items = []
        for idx, (line, label) in enumerate(valid):
            end = valid[idx + 1][0] if idx + 1 < len(valid) else len(lines)
            body = "\n".join(l for l in lines[line:end] if not re.fullmatch(r"\s*<<<PAGE_\d+>>>\s*", l))
            items.append({"path": tuple(label.split(".")), "text": body.strip(), "page_num": line_page[line]})

        chunks = []
        for leaf in items:
            if any(len(o["path"]) > len(leaf["path"]) and o["path"][: len(leaf["path"])] == leaf["path"] for o in items):
                continue
            ancestors = [it for it in items if it["path"] == leaf["path"][: len(it["path"])]]
            ancestors.sort(key=lambda it: len(it["path"]))
            pages = []
            for it in ancestors:
                if it["page_num"] not in pages:
                    pages.append(it["page_num"])
            chunks.append({
                "question_number": ".".join(leaf["path"]),
                "text": " ".join(it["text"] for it in ancestors if it["text"]),
                "page_num": leaf["page_num"],
                "page_nums": pages,
            })
        logger.info(f"AI segmentation fallback found {len(chunks)} question chunks")
        return chunks

    def process_image(
        self, filepath: str, subject_id: Optional[str] = None, board_id: Optional[str] = None,
        topic_id: Optional[str] = None,
    ) -> Dict:
        """Extract a single question from a standalone image (e.g. a
        screenshot), via OCR straight to the AI structuring step. Unlike
        process_pdf, there's no multi-page structure to segment — the whole
        image is treated as exactly one question, and no image region is
        auto-attached (a flat screenshot has none of the vector/text-block
        structure _collect_region_rects relies on to find one). If the
        screenshot itself needs to be attached — it has a diagram, say — the
        existing per-question image upload in the results view covers that."""
        logger.info(
            f"Starting image processing for: {filepath} (subject={subject_id}, board={board_id}, topic={topic_id})"
        )
        try:
            logger.info("Step 1: Running OCR on image...")
            image = Image.open(filepath)
            raw_text = pytesseract.image_to_string(image)
            logger.info(f"✓ OCR extracted {len(raw_text)} characters")

            logger.info("Step 2: Cleaning extracted text...")
            cleaned_text = self._clean_text(raw_text)
            logger.info(f"✓ Text cleaned. Characters after cleaning: {len(cleaned_text)}")

            if not cleaned_text.strip():
                logger.warning("No text detected in image")
                return {
                    "questions": [],
                    "metadata": {"total_pages": 1, "total_questions": 0, "end_page": 1, "has_more": False},
                }

            question_chunks = [{"text": cleaned_text, "page_num": 0, "page_nums": [0]}]

            bubble_lookup = build_bubble_lookup(subject_id, board_id, topic_id)
            if not bubble_lookup and topic_id:
                logger.warning("No bubbles matched the chosen topic; falling back to subject/board only.")
                bubble_lookup = build_bubble_lookup(subject_id, board_id)
            if not bubble_lookup and (subject_id or board_id):
                logger.warning("No bubbles matched the chosen subject/board; falling back to the full bubble list.")
                bubble_lookup = build_bubble_lookup()

            logger.info("Step 3: Processing question with AI...")
            questions = self._structure_with_ai(question_chunks, {}, {}, bubble_lookup)
            logger.info(f"✓ Processed {len(questions)} questions")

            logger.info("Step 4: Validating question data...")
            validated_questions = self._validate_questions(questions)
            logger.info(f"✓ Validated {len(validated_questions)} questions")

            result = {
                "questions": validated_questions,
                "metadata": {
                    "total_pages": 1,
                    "total_questions": len(validated_questions),
                    "start_page": 0,
                    "end_page": 1,
                    "has_more": False,
                },
            }

            logger.info("✅ Image processing completed successfully!")
            return result

        except Exception as e:
            logger.error(f"❌ Image processing failed: {str(e)}")
            raise Exception(f"Image processing failed: {str(e)}")

    # An embedded image is worth OCRing when it's big enough to hold
    # question text: at least this fraction of the page's width and height.
    OCR_MIN_IMAGE_WIDTH = 0.3
    OCR_MIN_IMAGE_HEIGHT = 0.05
    # ...and its OCR output is kept only if it reads like text — this many
    # real words (3+ letters). A photo yields next to none; even a single
    # question sub-part ("(i) Describe how the length of the sperm changes")
    # clears it.
    OCR_MIN_WORDS = 8
    OCR_MIN_CONFIDENCE = 50

    def _ocr_image_rows(self, page, page_num: int) -> List[dict]:
        """OCR every large image on the page that has no text layer of its
        own, as rows in page coordinates (same shape as _page_rows, plus
        "ocr": True) so they slot in among the page's real text by position.

        This covers a fully scanned page (one big image, no text), a
        compilation of question screenshots where only the question number
        or a source code is real text beside each image, and an ordinary
        page with one pasted-in screenshot — without deciding up front which
        kind of page this is. An image that already has real text over it
        (a searchable scan) is skipped, since that text is read directly."""
        rows: List[dict] = []
        width, height = page.rect.width, page.rect.height
        seen: List[fitz.Rect] = []
        for info in page.get_image_info():
            rect = fitz.Rect(info.get("bbox") or (0, 0, 0, 0)) & page.rect
            if rect.is_empty or rect.width < width * self.OCR_MIN_IMAGE_WIDTH or rect.height < height * self.OCR_MIN_IMAGE_HEIGHT:
                continue
            if any((rect & s).get_area() > 0.9 * rect.get_area() for s in seen):
                continue  # the same image placed twice
            seen.append(rect)
            if len(page.get_text("words", clip=rect)) > 5:
                continue

            try:
                # ~288 DPI — comfortably in Tesseract's recommended range.
                zoom = 4
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                # Tesseract's default layout analysis sometimes skips lines of a
                # mostly-blank question (answer space, dotted lines); reading
                # the image as one uniform block catches those but can garble
                # other layouts. Neither is better everywhere, so take the one
                # that reads more words and add any line only the other read.
                candidates = sorted(
                    [self._ocr_lines(img, rect, zoom, "--psm 3"), self._ocr_lines(img, rect, zoom, "--psm 6")],
                    key=lambda c: c[1],
                    reverse=True,
                )
            except Exception as e:
                logger.warning(f"Page {page_num}: OCR failed for an image, skipping it: {e}")
                continue

            image_rows, words = candidates[0]
            for extra in candidates[1][0]:
                if not any(
                    min(extra["bbox"].y1, r["bbox"].y1) - max(extra["bbox"].y0, r["bbox"].y0) > 0
                    for r in image_rows
                ):
                    image_rows.append(extra)
                    words += len(re.findall(r"[A-Za-z]{3,}", extra["text"]))
            if words >= self.OCR_MIN_WORDS:
                rows.extend(image_rows)
                logger.info(f"Page {page_num}: OCR'd {len(image_rows)} lines from an image with no text layer")
        return rows

    def _ocr_lines(self, img, rect: fitz.Rect, zoom: float, config: str) -> Tuple[List[dict], int]:
        """OCR `img` (a render of page area `rect` at `zoom`) into rows in
        page coordinates, plus how many real words (3+ letters) it read."""
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
        lines: Dict[Tuple[int, int, int], Dict] = {}
        for i, word in enumerate(data["text"]):
            word = word.strip()
            # Real text reads at ~90+ confidence; under 50 is almost always
            # Tesseract reading shapes as letters (a graph's grid, a photo).
            # A sub-part label like "(c)" is short and often scores low even
            # when read correctly — keep it regardless.
            if not word or (
                float(data["conf"][i]) < self.OCR_MIN_CONFIDENCE
                and not re.fullmatch(r"\((?:[a-h]|[ivx]{1,4})\)", word)
            ):
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x0 = rect.x0 + data["left"][i] / zoom
            y0 = rect.y0 + data["top"][i] / zoom
            box = fitz.Rect(x0, y0, x0 + data["width"][i] / zoom, y0 + data["height"][i] / zoom)
            entry = lines.setdefault(key, {"words": [], "bbox": fitz.Rect(box)})
            entry["words"].append(word)
            entry["bbox"] |= box
        rows = [
            {"text": " ".join(e["words"]), "x0": e["bbox"].x0, "y0": e["bbox"].y0,
             "bbox": e["bbox"], "bold": False, "ocr": True}
            for e in lines.values()
        ]
        words = sum(len(re.findall(r"[A-Za-z]{3,}", r["text"])) for r in rows)
        return rows, words

    def _extract_text(
        self, filepath: str, start_page: int = 0, end_page: Optional[int] = None,
        path_state: Optional[list] = None,
    ) -> tuple:
        logger.info(f"Opening PDF file: {filepath}")
        doc = fitz.open(filepath)
        total_pages = len(doc)
        end_page = min(end_page, total_pages) if end_page is not None else total_pages
        logger.info(f"PDF opened successfully. Total pages: {total_pages}. Extracting pages {start_page}-{end_page - 1}")

        full_text = ""
        page_image_map = {}   # {page_num: [(fitz.Rect, path), ...]}
        page_question_y = {}  # {page_num: {qnum_str: y0}}

        # Tracks state for the bare-number format across pages in document
        # order: [0] the current dotted path (e.g. ["1", "c"]), so a sub-part
        # marker on a later page (e.g. "(i)") can resolve its full path
        # ("1.c.i") even though its top-level number was set on an earlier
        # page; [1] the last confirmed top-level number, used to trust a
        # non-bold question number that exactly continues the sequence (see
        # _maybe_sequential_qstart) when a document bolds its question
        # numbers inconsistently; [2] the x-position of this document's
        # first bold-confirmed marker, used by that same fallback to tell a
        # real marker's column from same-shaped inline furniture elsewhere
        # on the page (see _match_top_level_marker/_MARKER_X0_TOLERANCE);
        # [3] whether this document numbers questions zero-padded ("01.1"),
        # which a later batch may have no "0X" number of its own to show
        # (see _normalize_padded_numbers).
        # Passed in from the previous batch when this is an "Extract More"
        # continuation, so numbering picks up where it left off; returned
        # (mutated) so the caller can hand it to the next batch.
        bare_path_state = path_state if path_state is not None else [[], None, None, False]

        for page_num in range(start_page, end_page):
            page = doc[page_num]
            # Computed once per page and reused below: _build_page_text_with_markers
            # needs it to tell a real bare question number from a graph's own
            # axis tick (see _maybe_sequential_qstart), and _extract_page_images
            # would otherwise just recompute the identical regions itself.
            region_rects = self._collect_region_rects(page, page_num)
            page_text, bare_q_y = self._build_page_text_with_markers(
                page, page_num, bare_path_state, region_rects
            )
            full_text += f"\n<<<PAGE_{page_num}>>>\n" + page_text

            images = self._extract_page_images(doc, page, page_num, region_rects)
            if images:
                page_image_map[page_num] = images
                logger.debug(f"Page {page_num}: extracted {len(images)} image(s)")

            # Record the y-position of each question number found on this page
            q_y = {}
            for block in page.get_text("blocks"):
                block_text = block[4].strip()
                m = re.match(r'^(0\d(?:\.\d)?)', block_text)
                if m and m.group(1) not in q_y:
                    q_y[m.group(1)] = block[1]  # y0 of the block
            q_y.update(bare_q_y)
            if q_y:
                page_question_y[page_num] = q_y

            logger.debug(f"Extracted text from page {page_num}: {len(page_text)} characters")

        logger.info(f"Text extraction complete. Total characters: {len(full_text)}")
        doc.close()
        logger.debug("PDF document closed")

        return full_text, total_pages, page_image_map, page_question_y, bare_path_state

    # A question number ("1", "2", "10") opening a row — alone, or followed
    # by the question's own text or a sub-part label on the same row — and
    # the "(a)"/"(i)" sub-part labels that can open a row or follow it.
    # Not when the next thing on the row is a lone digit: "1 0" is a two-digit
    # number printed one digit per box (see _normalize_padded_numbers).
    _BARE_QNUM_RE = re.compile(r"^([1-9]\d?)(?=[ \t]|$)(?![ \t]+\d(?:[ \t.]|$))")
    _SUBLETTER_RE = re.compile(r"^\(([a-h])\)")
    _SUBROMAN_RE = re.compile(r"^\(([ivx]{1,4})\)", re.IGNORECASE)

    # Two lines belong to the same visual row when their vertical extents
    # overlap by more than this fraction of the shorter one — loose enough
    # to keep a subscript/superscript (drawn as its own, offset line) on its
    # base row, tight enough that consecutive lines of a paragraph never merge.
    _ROW_OVERLAP = 0.5
    # A gap between two characters wider than this fraction of the font size
    # is a word break. Characters within a word, and a subscript tucked
    # against the letter before it, sit ~0pt apart; a word space is ~0.25em.
    _WORD_GAP = 0.15
    # A gap wider than this fraction of the page width splits a row in two.
    _ROW_SPLIT_GAP = 0.2
    # Real (non-OCR) words below the header that mark a page as text-based —
    # every text-based fixture page with real questions has more than this,
    # every scan or screenshot-compilation page well under (its only real
    # text is a "Paper 3" banner and the question numbers).
    _NATIVE_TEXT_PAGE_WORDS = 30

    def _page_rows(self, page) -> List[dict]:
        """The page's text as visual rows, top to bottom, each built from its
        individual characters ordered left to right.

        Built straight from PyMuPDF's character data rather than its
        block/line text, because how a PDF stores text often doesn't match
        how it looks: a question number drawn after the stem it sits to the
        left of, a subscript ("the 4 in CH4") stored as a separate line from
        its formula, or one visual line split across several blocks. Grouping
        by position puts each of those back where a reader sees it, and every
        row carries its own first-character font and position, so marker
        detection never has to match text from one source against font
        information from another.

        Each row: {"text", "x0", "y0", "bbox", "bold"} — bold/x0 describe the
        row's first character, i.e. the would-be question number."""
        lines = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                if abs(line["dir"][1]) > 0.1:
                    continue  # rotated margin text ("Do not write outside the box")
                chars = []
                for span in line["spans"]:
                    for ch in span["chars"]:
                        if ch["c"].strip():
                            chars.append((fitz.Rect(ch["bbox"]), ch["c"], span["font"], span["size"]))
                if chars:
                    lines.append({"bbox": fitz.Rect(line["bbox"]), "chars": chars})
        lines.sort(key=lambda l: (l["bbox"].y0, l["bbox"].x0))

        groups: List[dict] = []
        for line in lines:
            b = line["bbox"]
            for group in reversed(groups[-4:]):
                ref = group["first"]
                overlap = min(b.y1, group["bbox"].y1) - max(b.y0, group["bbox"].y0)
                if overlap > self._ROW_OVERLAP * min(b.height, ref.height):
                    group["chars"].extend(line["chars"])
                    group["bbox"] |= b
                    break
            else:
                groups.append({"chars": list(line["chars"]), "bbox": fitz.Rect(b), "first": fitz.Rect(b)})

        rows = []
        for group in groups:
            chars = sorted(group["chars"], key=lambda c: (c[0].x0, c[0].y0))
            # Text at the same height but far apart (a margin note like "Do
            # not write outside the box", a second column) is a separate row.
            segments = [[chars[0]]]
            for ch in chars[1:]:
                if ch[0].x0 - segments[-1][-1][0].x1 > page.rect.width * self._ROW_SPLIT_GAP:
                    segments.append([ch])
                else:
                    segments[-1].append(ch)
            for seg in segments:
                parts = []
                prev = None
                for rect, c, font, size in seg:
                    if prev is not None and rect.x0 - prev[0].x1 > self._WORD_GAP * max(size, prev[3]):
                        parts.append(" ")
                    parts.append(c)
                    prev = (rect, c, font, size)
                bbox = fitz.Rect(seg[0][0])
                for ch in seg[1:]:
                    bbox |= ch[0]
                rows.append({
                    "text": "".join(parts),
                    "x0": seg[0][0].x0,
                    "y0": bbox.y0,
                    "bbox": bbox,
                    "bold": "bold" in seg[0][2].lower(),
                })
        rows.sort(key=lambda r: (r["y0"], r["x0"]))
        return rows

    def _build_page_text_with_markers(
        self, page, page_num: int, path_state: list, region_rects: Optional[List[fitz.Rect]] = None
    ) -> Tuple[str, Dict[str, float]]:
        """This page's text in reading order (see _page_rows), one line per
        visual row, with sentinels spliced in front of question-number and
        sub-part markers for papers that number questions "1", "2" with
        "(a)"/"(i)" sub-parts rather than the zero-padded "01"/"01.1" scheme
        (see _splice_row_markers). Also returns {dotted_path: y0} for every
        marker found here (e.g. "1.c.i"), so images can later be matched to
        questions by vertical position.

        `path_state` ([path, last_num, marker_x0]) is carried across pages —
        and across "Extract More" batches — by the caller, so a sub-part on
        a later page still resolves its full path ("1.c.i") and the
        sequential fallback knows which number comes next.

        `region_rects` is this page's detected image/graph regions, used to
        tell a real question number alone on its row from a graph's axis tick
        (see _maybe_sequential_qstart)."""
        q_y: Dict[str, float] = {}
        page_height = page.rect.height

        # A table's cell text (e.g. a grid of MC options like "A carbon
        # oxygen nitrogen B hydrogen carbon oxygen...") reads exactly like a
        # list of labelled options once flattened to plain text, so the AI
        # tends to parse it as such — producing garbled multi-word "options"
        # instead of recognizing a table that belongs in an attached image
        # (already captured separately; see _collect_region_rects). Replace
        # each table's text with a one-time placeholder instead.
        table_rects = self._find_table_rects(page)
        tables_announced = set()

        native_rows = self._page_rows(page)
        ocr_rows = self._ocr_image_rows(page, page_num)
        # On a page whose questions are real text, an OCR'd image is a figure
        # (labels, axis numbers) — keep its words, but don't let a "2" or an
        # "(a)" inside it start a question. Only on a page that's essentially
        # all image (a scan, or screenshots with just a number beside each)
        # is OCR'd text where the question structure actually lives.
        native_words = sum(
            len(re.findall(r"[A-Za-z]{3,}", r["text"])) for r in native_rows if r["y0"] > 40
        )
        if native_words >= self._NATIVE_TEXT_PAGE_WORDS:
            for r in ocr_rows:
                r["no_markers"] = True
        rows = native_rows + ocr_rows
        rows.sort(key=lambda r: r["y0"])
        # A question number printed as real text beside a screenshot of its
        # question sits level with the screenshot's first OCR'd line, a hair
        # above or below it — on a shared row, left comes first.
        for i in range(len(rows) - 1):
            a, b = rows[i], rows[i + 1]
            overlap = min(a["bbox"].y1, b["bbox"].y1) - max(a["bbox"].y0, b["bbox"].y0)
            if overlap > self._ROW_OVERLAP * min(a["bbox"].height, b["bbox"].height) and b["x0"] < a["x0"]:
                rows[i], rows[i + 1] = b, a

        out = []
        for row in rows:
            center = fitz.Point((row["bbox"].x0 + row["bbox"].x1) / 2, (row["bbox"].y0 + row["bbox"].y1) / 2)
            table_idx = next((i for i, t in enumerate(table_rects) if center in t), None)
            if table_idx is not None:
                if table_idx not in tables_announced:
                    tables_announced.add(table_idx)
                    out.append("[table — see attached image]")
                continue

            text = row["text"]
            if row["y0"] <= page_height - self._FOOTER_MARGIN and not row.get("no_markers"):
                text, labels = self._splice_row_markers(
                    text, path_state, row["y0"], x0=row["x0"], bold=row["bold"],
                    strict_trailing=not row.get("ocr"), region_rects=region_rects,
                )
                for label, y0 in labels:
                    q_y.setdefault(label, y0)
            out.append(text)
        return "\n".join(out) + "\n", q_y

    # How close to the page's bottom edge counts as a running footer (e.g. a
    # page number) rather than page content. Chosen with margin above a real
    # observed footer page-number at ~64pt from the bottom edge. Deliberately
    # bottom-only, not top-and-bottom: a real question number has been seen
    # starting as little as ~49pt from the top of its page, so excluding a
    # symmetric top margin on unproven suspicion of a header one day would
    # cost more (silently dropping real questions) than it defends against.
    _FOOTER_MARGIN = 70

    # How far a candidate bare-number marker's left edge may drift from this
    # document's established question-number column (see _match_top_level_marker)
    # before it's treated as inline furniture rather than a real marker.
    # Observed real markers land within ~1pt of each other across pages of
    # the same document; an observed false positive (a chemical formula's
    # subscript count) sat 44pt further right. 20pt sits well clear of both.
    _MARKER_X0_TOLERANCE = 20

    def _splice_row_markers(
        self,
        text: str,
        path_state: list,
        y0: float,
        x0: Optional[float] = None,
        bold: bool = False,
        strict_trailing: bool = True,
        region_rects: Optional[List[fitz.Rect]] = None,
    ) -> Tuple[str, List[Tuple[str, float]]]:
        """Splice sentinels in front of the markers opening one row of text:
        a question number, then any sub-part labels right after it on the
        same row ("1 (c) (i) Name the..."), or sub-part labels alone.

        A bold question number is trusted outright. A plain-weight one goes
        through _maybe_sequential_qstart's guards instead — some papers don't
        bold their numbers at all, or only some of them. Sub-part labels are
        only taken from a bold row, or right after a question number: a
        plain "(a)" opening a row is usually prose (a classification key's
        "(a) has legs") rather than a sub-part. `strict_trailing=False` is
        for OCR'd text, which has no font information at all, so there every
        row is treated as potentially a marker row (see _ocr_image_rows)."""
        lead_ws = text[: len(text) - len(text.lstrip())]
        rest = text.lstrip()

        if bold:
            head, labels = self._match_top_level_marker(rest, path_state, y0, x0)
        else:
            head, labels = self._maybe_sequential_qstart(
                rest, path_state, y0, strict_trailing=strict_trailing, x0=x0, region_rects=region_rects
            )

        repeated_number = False
        if not labels and path_state[1] is not None:
            # A screenshot of a question often includes its own number, which
            # duplicates the number printed as real text beside it ("2" then
            # "2 (a) (i) State the meaning..."): skip the repeat, keep the labels.
            # OCR often misreads that repeat ("9" as "Q", "17" as "7"), so for
            # OCR'd text any short token will do — except an MC option letter
            # ("A (i) only").
            number = rf"{path_state[1]}\.?" if strict_trailing else r"(?![A-D]\b)[^\s(]{1,3}"
            m = re.match(rf"{number}[ \t]+(?=\()", rest)
            if m:
                rest = rest[m.end():]
                repeated_number = True

        if labels:
            m = re.match(r"(<<<QSTART_\w+>>>\d+)[ \t]*(.*)", head, re.S)
            tail, sub_labels = self._match_subpart_marker(m.group(2), path_state, y0)
            return f"{lead_ws}{m.group(1)}\n{tail}".rstrip(), labels + sub_labels
        if bold or not strict_trailing or repeated_number:
            spliced, sub_labels = self._match_subpart_marker(rest, path_state, y0)
            return f"{lead_ws}{spliced}", sub_labels
        return text, []

    def _maybe_sequential_qstart(
        self,
        line: str,
        path_state: list,
        y0: float,
        strict_trailing: bool = True,
        x0: Optional[float] = None,
        region_rects: Optional[List[fitz.Rect]] = None,
    ) -> Tuple[str, List[Tuple[str, float]]]:
        """A non-bold line can still be trusted as a genuine top-level
        question marker when the number it starts with exactly continues the
        sequence from the last confirmed question (or is "1", bootstrapping
        the very first one) — real question numbers always increment by
        exactly 1, so a coincidental false positive (an axis label, a mark
        total) landing on precisely the right next integer is unlikely,
        especially stacked on top of the gap filter in
        _segment_bare_numbered_questions, which independently rejects a
        tightly-packed run of digits (an answer-choice box) regardless of how
        each one got flagged. This recovers documents seen in practice where
        only SOME question numbers happen to be bold — e.g. a 13-question
        paper where just one number was bold and the rest were plain weight,
        which left every question but that one undetected. Deliberately
        narrower than the bold path: no sub-part (letter/roman) matching
        here, since a lettered sub-part has no comparably strong signal to
        lean on without a font cue, and no "(a)" shape is nearly as prone to
        coincidental collision as a bare digit anyway.

        `strict_trailing` controls whether a *continuation* (not the
        bootstrap) is required to have real same-line word content, not just
        a non-punctuation remainder. True (real PDF text) rules out a bare
        axis-tick value sitting alone on its own line — a real document was
        found where this costs something real: question numbers consistently
        alone on their own line, stem wrapping to the next line/block below.
        A same-line word is still accepted outright when present; lacking
        one, `region_rects` (this page's already-detected image/graph
        regions) breaks the tie — a number inside one of them is almost
        certainly a chart's own axis tick, while a number nowhere near any
        detected region has nothing else it could plausibly be. `x0`/`y0`
        together place the line for that containment check; `region_rects`
        left as None (as opposed to an empty list, meaning "checked, no
        regions on this page") keeps the old, stricter reject-outright
        behavior for any caller that hasn't been taught to supply it. False
        (OCR, which has no font info at all and frequently isolates even a
        genuine number as its own detected line, with the real question text
        landing on the next one) would otherwise reject real continuations —
        OCR accepts a bit more false-positive risk here in exchange, and
        ignores region_rects entirely since strict_trailing already short-
        circuits past the check that uses it.
        """
        rest = line.lstrip()
        m = self._BARE_QNUM_RE.match(rest.rstrip())
        if not m:
            return line, []
        num = int(m.group(1))
        last_num = path_state[1]
        is_bootstrap = last_num is None and num == 1
        if not is_bootstrap and (last_num is None or num != last_num + 1):
            return line, []

        trailing = rest[m.end():].strip()
        # A blank line for a written-in answer ("State two features: 1 ....
        # 2 ....") has exactly this shape too — reject when what follows is
        # nothing but dots/underscores filling space to write in, regardless
        # of bootstrap-vs-continuation or strictness.
        if trailing and re.fullmatch(r"[.…_\-\s]*", trailing):
            return line, []
        if not is_bootstrap and strict_trailing and not re.search(r"[A-Za-z]{2,}", trailing):
            if region_rects is None or x0 is None:
                return line, []
            in_region = any(
                rect.x0 <= x0 <= rect.x1 and rect.y0 <= y0 <= rect.y1
                for rect in region_rects
            )
            if in_region:
                return line, []
            # A real question number sits in a consistent left-hand column
            # page after page — once a bold marker has established where
            # that column is (see _match_top_level_marker), a same-shaped digit
            # sitting well off to the side of it (e.g. a chemical formula's
            # subscript count, inline mid-sentence) is furniture, not a
            # marker, even with no image region to blame it on.
            established_x0 = path_state[2] if len(path_state) > 2 else None
            if established_x0 is not None and abs(x0 - established_x0) > self._MARKER_X0_TOLERANCE:
                return line, []

        lead_ws = line[: len(line) - len(line.lstrip())]
        path_state[0] = [m.group(1)]
        path_state[1] = num
        return f"{lead_ws}<<<QSTART_{m.group(1)}>>>{rest}", [(m.group(1), y0)]

    def _match_top_level_marker(
        self, line: str, path_state: list, y0: float, x0: Optional[float] = None
    ) -> Tuple[str, List[Tuple[str, float]]]:
        """A bold question number opening `line`, trusted outright. Returns
        the line with a QSTART sentinel spliced in front, or unchanged with
        no labels if it doesn't open with a number."""
        m = self._BARE_QNUM_RE.match(line)
        if not m:
            return line, []
        path_state[0] = [m.group(1)]
        path_state[1] = int(m.group(1))
        # A bold, trusted marker is the best evidence of where this
        # document's question numbers sit horizontally — record it (once)
        # for _maybe_sequential_qstart to check plain-weight candidates against.
        if len(path_state) > 2 and path_state[2] is None and x0 is not None:
            path_state[2] = x0
        return f"<<<QSTART_{m.group(1)}>>>{line}", [(m.group(1), y0)]

    def _match_subpart_marker(
        self, line: str, path_state: list, y0: float
    ) -> Tuple[str, List[Tuple[str, float]]]:
        """Letter/roman sub-part labels opening `line` (no question-number
        check) — see _splice_row_markers, which calls this both after a
        question number and on rows that don't open with one."""
        lead_ws = line[: len(line) - len(line.lstrip())]
        rest = line.lstrip()
        labels: List[Tuple[str, float]] = []

        # A letter and roman marker can share one line (e.g. "(a) (i) Name the
        # group..." when (a) has no text of its own), so strip them off in a
        # loop rather than just checking the very first one.
        sentinels = ""
        while True:
            m = self._SUBLETTER_RE.match(rest)
            if m:
                if not path_state[0]:
                    break  # a letter marker with no top-level context yet
                path_state[0] = [path_state[0][0], m.group(1)]
                labels.append((".".join(path_state[0]), y0))
                sentinels += f"<<<SUBLETTER_{m.group(1)}>>>"
                rest = re.sub(r"^\([a-h]\)[ \t]*", "", rest, count=1)
                continue
            m = self._SUBROMAN_RE.match(rest)
            if m:
                if not path_state[0]:
                    break
                # Nest under the current letter if there is one; a roman with
                # no letter above it (or whose letter OCR missed) sits
                # directly under the question number.
                base = path_state[0][:2] if len(path_state[0]) > 1 and re.fullmatch(r"[a-h]", path_state[0][1]) else path_state[0][:1]
                path_state[0] = base + [m.group(1).lower()]
                labels.append((".".join(path_state[0]), y0))
                sentinels += f"<<<SUBROMAN_{m.group(1).lower()}>>>"
                rest = re.sub(r"^\([ivxIVX]{1,4}\)[ \t]*", "", rest, count=1)
                continue
            break

        if sentinels:
            return f"{lead_ws}{sentinels}{rest}", labels
        return line, []

    _FIG_LABEL_RE = re.compile(r"Fig\.?\s*(\d+\.\d+)", re.IGNORECASE)

    def _nearest_figure_label(self, page, rect: fitz.Rect, max_dist: float = float("inf")) -> Optional[str]:
        """Find the closest "Fig X.Y" mention on the page to this image's rect
        (a short caption right below it, or an intro sentence right above it —
        and, for a tall composite figure split into several boxes, possibly
        far below the topmost ones: a caption at the bottom of a multi-row
        species grid can be 600+pt from the top row, so there's no safe fixed
        cutoff — unmatched regions don't get merged into their real figure at
        all, which is worse than an occasional wrong match on a crowded page).
        Matching images to questions by this label is far more precise than a
        vertical-position heuristic, and — unlike a position-based match —
        isn't fooled when the same label (e.g. "Fig. 1.1") is reused by an
        unrelated image elsewhere, as happens in a compiled worksheet where
        each original question restarts its own figure numbering at 1.1."""
        best_label = None
        best_dist = max_dist
        for block in page.get_text("blocks"):
            m = self._FIG_LABEL_RE.search(block[4])
            if not m:
                continue
            brect = fitz.Rect(block[:4])
            if brect.y1 <= rect.y0:
                dist = rect.y0 - brect.y1
            elif brect.y0 >= rect.y1:
                dist = brect.y0 - rect.y1
            else:
                dist = 0
            if dist < best_dist:
                best_dist = dist
                best_label = m.group(1)  # e.g. "2.2"
        return best_label

    def _extract_page_images(
        self, doc, page, page_num: int, region_rects: Optional[List[fitz.Rect]] = None
    ) -> List[tuple]:
        """Return (fitz.Rect, path, fig_label) tuples for all images, tables, and boxes on the page.
        `region_rects`, if the caller already has this page's _collect_region_rects
        result (e.g. _extract_text, which also needs it for marker detection),
        is reused instead of recomputing it here."""
        images_dir = os.path.join(os.path.dirname(__file__), "images")
        os.makedirs(images_dir, exist_ok=True)
        seen_rects = []
        candidates = []  # [(rect, kind, xref), ...] — not yet saved to disk

        # Build xref -> on-page bounding rect from image placement info
        xref_to_rect = {}
        try:
            for info in page.get_image_info(xrefs=True):
                xref = info.get("xref", 0)
                bbox = info.get("bbox")
                if xref and bbox:
                    xref_to_rect[xref] = fitz.Rect(bbox)
        except Exception:
            pass

        # Embedded raster images (photos, diagrams, graphs)
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.width < 100 or pix.height < 100:
                    continue
            except Exception as e:
                logger.warning(f"Failed to read image from page {page_num}: {e}")
                continue
            rect = xref_to_rect.get(xref, fitz.Rect(0, 0, page.rect.width, page.rect.height))
            seen_rects.append(rect)
            candidates.append((rect, "raster", xref))

        # Vector regions: word banks, tables, bordered diagrams
        if region_rects is None:
            region_rects = self._collect_region_rects(page, page_num)
        for rect in region_rects:
            # Deduplicate against raster images and previously collected vector regions
            is_dup = False
            for seen in seen_rects:
                inter = rect & seen
                if not inter.is_empty:
                    inter_area = inter.width * inter.height
                    min_area = min(rect.width * rect.height, seen.width * seen.height)
                    if min_area > 0 and inter_area / min_area > 0.8:
                        is_dup = True
                        break
            if is_dup:
                continue
            seen_rects.append(rect)
            candidates.append((rect, "vector", None))

        # A figure can be laid out across several separately-bordered boxes
        # that are still one logical figure (e.g. a species-identification
        # grid split into two rows, each its own box) — group candidates by
        # the "Fig X.Y" label nearest each one and save a group sharing a
        # label as a single merged crop instead of several disjoint pieces.
        groups: Dict[str, List[Tuple[fitz.Rect, str, Optional[int]]]] = {}
        solo: List[Tuple[fitz.Rect, str, Optional[int]]] = []
        for rect, kind, xref in candidates:
            label = self._nearest_figure_label(page, rect)
            if label:
                groups.setdefault(label, []).append((rect, kind, xref, label))
            else:
                solo.append((rect, kind, xref, label))

        saved = []
        for label, members in groups.items():
            if len(members) == 1:
                solo.append(members[0])
                continue
            union = members[0][0]
            for rect, _, _, _ in members[1:]:
                union |= rect
            path = self._save_region_crop(page, union, images_dir)
            if path:
                saved.append((union, path, label))
                logger.debug(
                    f"Page {page_num}: merged {len(members)} regions sharing Fig {label} "
                    f"into one {union.width:.0f}×{union.height:.0f}pt image"
                )

        for rect, kind, xref, label in solo:
            if kind == "raster":
                path = self._save_raster_image(doc, xref, images_dir)
            else:
                path = self._save_region_crop(page, rect, images_dir)
                if path:
                    logger.debug(f"Page {page_num}: vector region {rect.width:.0f}×{rect.height:.0f}pt saved")
            if path:
                saved.append((rect, path, label))

        return saved

    def _save_raster_image(self, doc, xref: int, images_dir: str) -> Optional[str]:
        try:
            pix = fitz.Pixmap(doc, xref)
            # PNG only supports DeviceGray/DeviceRGB. CMYK (n>3) obviously needs
            # conversion, but so do "n==1" colorspaces that aren't actually
            # DeviceGray — e.g. a Separation/spot-color channel (common for
            # monochrome scans like an electron micrograph), which PyMuPDF
            # still refuses to save as-is.
            if pix.colorspace and pix.colorspace not in (fitz.csGRAY, fitz.csRGB):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            filename = f"{uuid.uuid4().hex}.png"
            pix.save(os.path.join(images_dir, filename))
            return f"/api/images/{filename}"
        except Exception as e:
            logger.warning(f"Failed to save raster image: {e}")
            return None

    def _save_region_crop(self, page, rect: fitz.Rect, images_dir: str) -> Optional[str]:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect)
        filename = f"{uuid.uuid4().hex}.png"
        pix.save(os.path.join(images_dir, filename))
        return f"/api/images/{filename}"

    def _find_table_rects(self, page) -> List[fitz.Rect]:
        """Find real tables on this page via PyMuPDF's table finder, filtering
        out both noise (too small to be a real table) and a different
        failure mode in the other direction: on a page with several unrelated
        aligned-text structures (e.g. one genuine small table plus some
        option-lettering columns elsewhere), the heuristic table finder can
        merge everything it sees into one "table" spanning nearly the whole
        page. A real MC-option table has been observed at ~20% of page
        height; capping at 60% comfortably allows a bigger real table while
        rejecting a whole-page false merge (observed at ~83%)."""
        page_width = page.rect.width
        page_height = page.rect.height
        rects = []
        try:
            for table in page.find_tables():
                rect = fitz.Rect(table.bbox)
                if (
                    not rect.is_empty
                    and rect.width >= page_width * 0.2
                    and rect.height >= 20
                    and rect.height <= page_height * 0.6
                ):
                    rects.append(rect)
        except AttributeError:
            pass  # find_tables() requires PyMuPDF >= 1.23
        except Exception as e:
            logger.warning(f"Table detection failed: {e}")
        return rects

    def _collect_region_rects(self, page, page_num: int) -> List[fitz.Rect]:
        """Collect unique candidate rects for word banks, tables, and bordered diagrams."""
        page_width = page.rect.width
        candidates = []

        # Drawing paths — sort largest area first so outer borders take priority over inner cells
        drawings = sorted(
            page.get_drawings(),
            key=lambda d: (d["rect"].width * d["rect"].height) if d.get("rect") else 0,
            reverse=True,
        )
        for drawing in drawings:
            if drawing.get("color") is None:  # no visible stroke
                continue
            rect = drawing.get("rect")
            if rect is None or rect.is_empty or rect.is_infinite:
                continue
            if rect.width < page_width * 0.2 or rect.height < 20:
                continue
            if not page.get_textbox(rect).strip():  # no text inside
                continue
            candidates.append(rect)

        # PyMuPDF table finder — catches tables whose borders are line
        # segments, not rect paths, or that have no drawn border at all (a
        # borderless table laid out with whitespace/alignment only). Run
        # before the cluster-based fallback below: the dedup pass at the end
        # of this method keeps whichever candidate was added first when two
        # overlap, so a precise, structurally-detected table needs to be in
        # the list before the speculative cluster scan has a chance to
        # engulf the same area in an imprecise, oversized region.
        for rect in self._find_table_rects(page):
            candidates.append(rect)
            logger.debug(f"Page {page_num}: table found at y={rect.y0:.0f}")

        # Line graphs (axes, gridlines, the plotted line itself) and shaded
        # illustrations (solid-fill shapes, no stroke) are drawn as dozens of
        # separate small paths with no single enclosing border, so they never
        # produce one qualifying rect above. Cluster the leftover paths
        # spatially and treat a big-enough cluster as a figure. A path can
        # carry a stroke color, a fill color, or both — requiring only
        # "color" here missed every fill-only shape, which silently dropped
        # whole sections of shaded (non-line-art) illustrations. No size
        # exclusion here either: a complex multi-path illustration (e.g. an
        # animal's outline) is sometimes rendered as a few large sub-paths
        # alongside many small ones — excluding the large ones because they
        # "should" have been caught above only works if they actually have
        # text inside, which an outline-only path never does, so they'd
        # otherwise fall into a gap neither path catches and crop the figure.
        # The final overlap-based dedup below collapses any rect this
        # clustering happens to re-find that the bordered-box pass above
        # already saved.
        thin_rects = [
            d["rect"] for d in drawings
            if (d.get("color") is not None or d.get("fill") is not None)
            and d.get("rect") is not None
            and not d["rect"].is_empty and not d["rect"].is_infinite
        ]
        for cluster in self._cluster_rects(thin_rects, pad=4):
            # A small diagram built from few, simple sub-paths (e.g. one
            # Lewis structure in a multi-option grid, whose symmetric dot
            # pairs sometimes collapse into fewer paths than a neighbouring
            # option's) can legitimately land as low as ~8 strokes — lower
            # than 10 risked dropping real, complete diagrams while still
            # rejecting the true noise case (a stray underline or a couple
            # of decorative strokes, typically 1-5 paths).
            if len(cluster) < 6:
                continue
            # An answer tick-box (a bare rectangle outline, no content) is
            # rendered by some documents as ~9 thin fill strips tracing its
            # 4 edges and corners, each a sliver in one dimension — clearing
            # the lowered stroke-count floor above without being a figure at
            # all. Real diagram content (an electron-dot blob, a shaded
            # illustration fill, an arrowhead, a labelled box) always has at
            # least one path with real area in both dimensions; a cluster
            # built entirely of slivers never does, so require one such path
            # before treating the cluster as a figure.
            if not any(min(r.width, r.height) >= 3 for r in cluster):
                continue
            union = cluster[0]
            for rect in cluster[1:]:
                union |= rect
            # Unlike the bordered-box candidates above, a cluster isn't
            # required to span 20% of the page width — a portrait-oriented
            # illustration (e.g. one species in a multi-species grid) can be
            # well under 120pt wide while still being a complete, legitimate
            # figure. The >=6-stroke requirement already rules out noise;
            # this is just a sanity floor against a stray fleck.
            if union.width < 20 or union.height < 20:
                continue
            # A decorative full-page background panel (a fill-only box
            # behind the whole text column, plus a few bullet-highlight
            # strips) is built from just enough paths to clear the floor
            # above once it's lowered to catch small diagrams, and its
            # paths all touch/overlap so they cluster as one "figure"
            # spanning nearly the entire page. A real figure this large in
            # both dimensions at once doesn't occur in these documents, so
            # reject it the same way the table finder already rejects a
            # whole-page false table merge.
            if union.width > page_width * 0.8 and union.height > page.rect.height * 0.8:
                continue

            # The bare plotted lines/gridlines don't cover axis numbers, axis
            # titles, the legend, or the "Fig. x.x" caption — those sit just
            # outside this box. Pull in nearby text blocks so the saved image
            # is a complete, labelled figure rather than bare unlabeled lines.
            # Margins are generous because the real gap to a caption varies a
            # lot between documents — but a generous search box will just as
            # happily catch the ordinary paragraph introducing the figure, or
            # a dichotomous key sitting right below it, as it will the actual
            # caption. Geometry alone can't tell those apart reliably (the gap
            # to an unrelated block is sometimes smaller than the gap to the
            # real caption), so only merge a nearby block if it reads like a
            # label/caption in the first place: short, and not a full
            # sentence (captions and axis labels don't end in '.'/'?'/'!';
            # question stems and key entries do, or run long).
            search_rect = fitz.Rect(
                union.x0 - 100, union.y0 - 15, union.x1 + 150, union.y1 + 60
            )
            for block in page.get_text("blocks"):
                brect = fitz.Rect(block[:4])
                if (brect & search_rect).is_empty:
                    continue
                block_text = block[4].strip()
                if not block_text:
                    continue
                is_caption = bool(self._FIG_LABEL_RE.search(block_text))
                # Check every line, not just the block's trailing characters —
                # a block like "To which class does it belong?\nA amphibians"
                # ends on a short answer-option line, but its first line is
                # still a real question sentence that must be excluded.
                block_lines = [l.strip() for l in block_text.split("\n") if l.strip()]
                is_label_like = len(block_text) <= 60 and not any(
                    l.endswith((".", "?", "!")) for l in block_lines
                )
                # An MC option ("A\namphibians") is exactly as short and as
                # free of sentence punctuation as a real axis/legend label
                # (e.g. "with toxin"), so the checks above can't tell them
                # apart — exclude the option-lettering shape explicitly.
                # Without this, merging one in extends the crop's bounds far
                # enough to geometrically sweep in the question stem sitting
                # between the figure and the options too, even though that
                # stem itself fails every check above.
                is_mc_option = bool(re.match(r"^[A-Da-d]\s*\n", block_text))
                if (is_caption or is_label_like) and not is_mc_option:
                    union |= brect

            candidates.append(union)

        # Deduplicate: prefer larger rects (already sorted that way for drawings)
        unique = []
        for rect in candidates:
            is_dup = False
            for seen in unique:
                inter = rect & seen
                if not inter.is_empty:
                    inter_area = inter.width * inter.height
                    min_area = min(rect.width * rect.height, seen.width * seen.height)
                    if min_area > 0 and inter_area / min_area > 0.8:
                        is_dup = True
                        break
            if not is_dup:
                unique.append(rect)

        return unique

    def _cluster_rects(self, rects: List[fitz.Rect], pad: float = 4) -> List[List[fitz.Rect]]:
        """Group rects into spatially-connected clusters: two rects (expanded
        by `pad`) are in the same cluster if they touch or overlap, transitively."""
        n = len(rects)
        expanded = [fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad) for r in rects]
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                i = parent[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                inter = expanded[i] & expanded[j]
                if not inter.is_empty:
                    pi, pj = find(i), find(j)
                    if pi != pj:
                        parent[pi] = pj

        groups: Dict[int, List[fitz.Rect]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(rects[i])
        return list(groups.values())

    def _clean_text(self, text: str) -> str:
        logger.info("Starting text cleaning process...")
        original_length = len(text)

        text = text.replace("\u00a0", " ")
        text = re.sub(r'[✓\uf0fc\uf0fe]|\(\)', '', text)
        text = re.sub(r"Figure\s*\d+", "The diagram below", text, flags=re.IGNORECASE)
        text = re.sub(r"\bTable\s*\d+", "The table below", text, flags=re.IGNORECASE)
        logger.debug("Replaced non-breaking spaces")

        # A line that's nothing but a run of dots (the ruled/dotted space
        # given for a handwritten answer in open-response questions, e.g.
        # "....................................................") carries
        # no content — unlike an underscore blank, it isn't a fill-in gap,
        # it's just writing space, so drop the whole line rather than
        # preserving it into the extracted question text. A multi-line
        # answer sometimes labels each ruled line — a bare "1 ...."/"2 ...."
        # for a two-part answer, or a short prompt word like "difference
        # ...." / "similarity ...." in a compare-and-contrast question —
        # that leading label is part of the same writing-space furniture,
        # not content the model needs, so it's optional and dropped along
        # with the dots. Capped at a couple of words (no punctuation inside)
        # so this can't eat a real sentence that happens to end a line; a
        # dot run followed by MORE text on the same line (a dot leader in a
        # table, e.g. "joined by a flap of skin ..... go to 2") still isn't
        # touched, since the match requires the dots to run to end of line.
        # A CAIE-style mark allocation ("....[2]") can trail the dots on the
        # same line — also furniture, not content, so it's optional too.
        text = re.sub(
            r"(?m)^[ \t]*(?:\(?[a-zA-Z0-9][a-zA-Z0-9 ]{0,20}\)?[ \t]+)?(?:\.[ \t]*){8,}(?:\[\s*\d+\s*\])?[ \t]*$\n?",
            "",
            text,
        )

        # Remove calculation answer placeholders ("Energy = _____ J", "mass:
        # .......... g"): a whole row of label, "=" or ":", a real blank to
        # write in, and optionally a unit. Confined to one row and requiring
        # an actual blank, so ordinary text with a colon ("An atom of element
        # Y has:") and the row break before the next question are untouched.
        text = re.sub(
            r"(?m)^[ \t]*[A-Za-z][A-Za-z ()]*[=:][ \t]*(?:_{3,}|\.{3,}|…+)[ \t._…]*[A-Za-z0-9/ ³²⁻]*\n?",
            "",
            text,
        )

        text = self._preserve_fill_in_blanks(text)

        # Define patterns to remove that are NOT used for fill-in-the-blank detection
        patterns_to_remove = [
            r"do\s*not\s*write\s*out\s*side\s*the\s*box",
            r"text\s*continues\s*on\s*the\s*next\s*page",
            r"Turn over",
            r"IB/M/\d+/\w+",
            r"IB/M/\w+",
            r"Copyright .*?\n?",
            r"\*\d+\*",
            r"►\s*/\d+/\w+/\w+",
            r"►\s*/\d+/\w+",
            r"►\s*/\w+",
            r"►\s*/\w+/\w+",
            r"►\s*/\w+/\w+/\w+",
            r"Tick\s*(?:\([^)]*\))?\s*(?:one|two|three|four|\d+)?\s*box(?:es)?\.?",
            r"Choose\s*one\s*option\.?",
            r"Select\s*one\s*answer\.?",
            r"Mark\s*one\s*answer\.?",
            r"Each\s*answer\s*may\s*be\s*used\s*once,\s*more\s*than\s*once\s*or\s*not\s*at\s*all\.?",
            r"Select\s*the\s*correct\s*option\.?",
            r"Choose\s*the\s*best\s*answer\.?",
            r"Identify\s*the\s*correct\s*statement\.?",
            r"Write\s*the\s*letter\s*in\s*the\s*box\.?",
            r"Choose\s*from\s*the\s*options\s*below\.?",
            r"Select\s*one\s*from\s*the\s*following\.?",
            r"do\s*not\s*write\s*on\s*this\s*page\.?",
            r"answer\s*in\s*the\s*spaces?\s*provided\.?",
        ]

        for i, pattern in enumerate(patterns_to_remove, 1):
            before_length = len(text)
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
            removed = before_length - len(text)
            if removed > 0:
                logger.debug(f"Pattern {i}: Removed {removed} characters")

        text = re.sub(r"\n\s*\d+\s*\n", "\n", text)
        text = re.sub(r"\n{2,}", "\n", text)
        text = re.sub(r"[ ]{2,}", " ", text)
        text = text.strip()

        cleaned_length = len(text)
        logger.info(
            f"Text cleaning complete. Reduced from {original_length} to {cleaned_length} characters ({original_length - cleaned_length} removed)"
        )
        return text

    def _preserve_fill_in_blanks(self, text: str) -> str:
        # Normalise all underscore sequences to consistent blanks.
        # Per-question blank removal for non-fill types happens later in _structure_with_ai.
        text = re.sub(r"_{3,}", "____", text)
        text = re.sub(r"(?:_\s+){2,}_?", "____", text)
        text = re.sub(r"(?<=\s)_(?=\s)", "____", text)
        text = re.sub(r"(?<=\s)_(?=[.,;:])", "____", text)
        text = re.sub(r"(?<=[.,;:])\s*_(?=\s)", "____", text)
        return text

    def is_fill_the_gap_question(self, text: str) -> bool:
        logger.debug("Raw question text before fill-the-gap check:\n%s", text)
        logger.debug("Checking if question is fill-the-gap...")
        logger.debug(f"Checking gap phrases in: '{text[:50]}...'")
        gap_phrases = [
            "complete the sentence", 
            "fill in the blank", 
            "fill in the gap", 
            "choose answers from the box", 
            "write the correct word",
            "select the correct word",
            "choose the correct word",
            "write down the correct word",
            "write the missing word",
            "choose from the box",
            "select from the box",
            "fill the gaps",
            "fill the blanks",
        ]

        text_lower = text.lower()
        return any(phrase in text_lower for phrase in gap_phrases)

    def _detect_question_type(self, text: str) -> str:
        text_lower = text.lower()

        # Check for fill-in-the-blank indicators (only by phrases now)
        if self.is_fill_the_gap_question(text):
            return "Fill in the Blank"

        # Rest of the method remains the same...
        if any(phrase in text_lower for phrase in ["tick", "choose", "select", "circle"]):
            return "Multiple Choice"

        if re.search(r"\([A-E]\)|\b[A-E]\.", text):
            return "Multiple Choice"

        return "Short Answer"

    def _normalize_padded_numbers(self, text: str, padded: bool = False) -> Tuple[str, bool]:
        """Zero-padded papers (AQA style "01", "01.1") print each digit of a
        question number in its own box, which extracts as "0 1 . 1". Join
        those back up at the start of a row, where question numbers sit.
        Returns the text and whether it uses this scheme at all — only then
        are "1 0"/"1 0 . 1" (question 10 onwards) joined too, since in an
        unpadded paper a row opening "1 0" is ordinary content."""
        text = re.sub(r"(?m)^([ \t]*)0[ \t]*([1-9])[ \t]*\.[ \t]*(\d{1,2})(?=\s)", r"\g<1>0\2.\3", text)
        text = re.sub(r"(?m)^([ \t]*)0[ \t]*([1-9])(?=\s)", r"\g<1>0\2", text)
        padded = padded or bool(re.search(r"(?m)^[ \t]*0[1-9](?:\.\d{1,2})?[ \t]+[A-Za-z(]", text))
        if padded:
            text = re.sub(r"(?m)^([ \t]*)([1-3])[ \t]*(\d)[ \t]*\.[ \t]*(\d{1,2})(?=\s)", r"\g<1>\2\3.\4", text)
            text = re.sub(r"(?m)^([ \t]*)([1-3])[ \t]+(\d)(?=[ \t]+[A-Za-z(])", r"\g<1>\2\3", text)
        return text, padded

    def _segment_questions(
        self, text: str, last_num: Optional[int] = None, padded: bool = False
    ) -> Tuple[List[Dict], Optional[int], bool]:
        """Split text into question chunks. Returns (chunks, restart_page,
        padded): whether this is a zero-padded ("01.1") paper, to pass back
        in as `padded` for the document's next batch along with `last_num`,
        the last question number confirmed so far — a later batch may hold
        only question 10 onwards, with no "0X" number to recognise the
        scheme by, and its sequence check (below) continues from there."""
        logger.info("Segmenting text into question chunks...")

        text, padded = self._normalize_padded_numbers(text, padded)
        # A zero-padded question number opens its row and is followed by the
        # question's own words (or a sub-part label) — which rules out a page
        # number on its own row ("35"), a paper code ("0610/11/O/N/24") and
        # the like, all of which otherwise look the same.
        marker_re = re.compile(r"(?m)^[ \t]*((?:0[1-9]|[1-3]\d)(?:\.\d{1,2})?)(?=[ \t]+[A-Za-z(])")
        chunks = []
        if padded:
            # A real number only ever moves on to the next question or to a
            # later part of the current one — anything else opening a row
            # with the same shape (a graph's "30.0" axis value beside its
            # label, a "25" in a table) is content, not a question boundary.
            starts = []
            top, sub = last_num, None
            for m in marker_re.finditer(text):
                t, _, s = m.group(1).partition(".")
                t, s = int(t), int(s) if s else None
                if top is None or t == top + 1 or (t == top and s is not None and (sub is None or s > sub)):
                    starts.append(m.start(1))
                    top, sub = t, s
            chunks = [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]

        logger.info(f"Found {len(chunks)} question chunks using regex pattern")

        if len(chunks) <= 1:
            # Zero matches means no zero-padded "01"/"02" markers at all. But
            # even ONE match is a weak signal worth cross-checking: a decimal
            # value like "0.04%" sitting alone on its own line (a common MC
            # option layout) looks identical to a genuine zero-padded question
            # number, and since it's the only "match", it swallows the entire
            # rest of the document into one bogus chunk instead of correctly
            # finding nothing. Cross-check against the bare, unpadded
            # numbering ("1", "2", ...) with "(a)"/"(i)" sub-parts, flagged
            # during extraction (see _build_page_text_with_markers) as
            # "<<<QSTART_n>>>"/"<<<SUBLETTER_x>>>"/"<<<SUBROMAN_x>>>"
            # sentinels, and prefer whichever path finds more structure.
            bare_chunks, restart_page = self._segment_bare_numbered_questions(text)
            if len(bare_chunks) > len(chunks):
                logger.info(
                    f"Found {len(bare_chunks)} question chunks using bare-number fallback "
                    f"(preferred over {len(chunks)} from regex pattern)"
                )
                return bare_chunks, restart_page, padded
            if not chunks:
                logger.info(f"Found {len(bare_chunks)} question chunks using bare-number fallback")
                return bare_chunks, restart_page, padded

        # Build a lookup of character positions to page numbers using markers
        page_positions = []
        for m in re.finditer(r"<<<PAGE_(\d+)>>>", text):
            page_positions.append((m.start(), int(m.group(1))))

        def get_page_for_pos(pos: int) -> int:
            page = 1
            for marker_pos, page_num in page_positions:
                if marker_pos <= pos:
                    page = page_num
                else:
                    break
            return page

        # First pass: parse every chunk's question number, clean text, and page.
        parsed = []
        search_start = 0
        for raw_chunk in chunks:
            current_chunk = raw_chunk.strip()
            qnum_match = re.match(r"^((?:0[1-9]|[1-3]\d)(?:\.\d{1,2})?)", current_chunk)
            qnum = qnum_match.group(1) if qnum_match else None

            chunk_pos = text.find(current_chunk[:40], search_start)
            page_num = get_page_for_pos(chunk_pos) if chunk_pos != -1 else 1
            search_start = chunk_pos + 1 if chunk_pos != -1 else search_start

            clean_text = re.sub(
                r"<<<PAGE_\d+>>>|<<<QSTART_[\w-]+>>>|<<<SUBLETTER_[a-h]>>>|<<<SUBROMAN_[a-z]+>>>", "", current_chunk
            ).strip()
            parsed.append({"question_number": qnum, "text": clean_text, "page_num": page_num})

        # Second pass: a bare base number (e.g. "01") immediately followed by its own
        # ".1" sub-part is a shared stem, not a standalone question — prepend its text
        # to EVERY sub-part sharing that base number (.1, .2, .3, ...), not just the first.
        merged_chunks = []
        i = 0
        while i < len(parsed):
            item = parsed[i]
            qnum = item["question_number"]
            is_stem_only = bool(qnum) and "." not in qnum

            if is_stem_only and i + 1 < len(parsed):
                next_qnum = parsed[i + 1]["question_number"]
                if next_qnum and next_qnum.split(".")[0] == qnum and next_qnum.endswith(".1"):
                    stem_text = item["text"]
                    j = i + 1
                    while j < len(parsed) and parsed[j]["question_number"] and parsed[j]["question_number"].split(".")[0] == qnum:
                        merged_chunks.append({
                            "question_number": parsed[j]["question_number"],
                            "text": f"{stem_text} {parsed[j]['text']}",
                            "page_num": parsed[j]["page_num"],
                        })
                        j += 1
                    i = j
                    continue

            merged_chunks.append(item)
            i += 1

        logger.info(f"After merging, total question chunks: {len(merged_chunks)}")
        return merged_chunks, None, padded

    def _segment_bare_numbered_questions(self, text: str) -> Tuple[List[Dict], Optional[int]]:
        """Segment a paper that numbers questions with plain integers ('1', '2',
        ...) and letters/romans for sub-parts ('(a)', '(i)') instead of CAIE's
        zero-padded '01'/'01.1' scheme, by splitting on the sentinels spliced in
        during extraction (see _build_page_text_with_markers).

        Generalizes the official format's stem-merge (a bare '01' immediately
        followed by its own '.1' is shared context, not a standalone question)
        to this format's two levels of nesting: every marker that has deeper
        markers nested under it is a stem, and its text is prepended to every
        leaf beneath it. A leaf is a marker with nothing nested under it."""
        page_positions = [(m.start(), int(m.group(1))) for m in re.finditer(r"<<<PAGE_(\d+)>>>", text)]

        def get_page_for_pos(pos: int) -> int:
            page = 1
            for marker_pos, page_num in page_positions:
                if marker_pos <= pos:
                    page = page_num
                else:
                    break
            return page

        marker_re = re.compile(r"<<<QSTART_([\w-]+)>>>|<<<SUBLETTER_([a-h])>>>|<<<SUBROMAN_([a-z]+)>>>")
        raw_matches = list(marker_re.finditer(text))

        # A bold, standalone digit isn't always a question number — an MC
        # answer-choice box (e.g. "1  9  10  19  28") prints each option in
        # bold on its own line too, and those sit only ~2-20 characters apart
        # from their neighbors. Real questions, even short back-to-back MC
        # ones, are consistently 80+ characters apart (number + stem + a few
        # lettered options). 40 sits well clear of both observed ranges.
        # Drop any QSTART candidate within that distance of another one so a
        # tightly packed box of digits can't be mistaken for question
        # boundaries.
        MIN_QSTART_GAP = 40
        qstart_positions = [m.start() for m in raw_matches if m.group(1) is not None]

        def qstart_is_isolated(pos: int) -> bool:
            return all(abs(p - pos) >= MIN_QSTART_GAP for p in qstart_positions if p != pos)

        matches = [m for m in raw_matches if m.group(1) is None or qstart_is_isolated(m.start())]
        if not any(m.group(1) is not None for m in matches):
            return [], None
        # Drop any sub-part marker before the first surviving top-level number —
        # it can't be meaningfully nested without one.
        first_qstart = next(i for i, m in enumerate(matches) if m.group(1) is not None)
        matches = matches[first_qstart:]

        # Two top-level markers in a row sharing the EXACT same number (not a
        # drop to a smaller one) are almost never a genuine restart — a real
        # new section restarts at a clearly smaller number, typically back to
        # 1. An exact repeat instead means the same question got marked
        # twice: once by noise the non-bold sequential fallback picked up
        # (a numbered statement in a list, an MC option's own leading digit
        # with a trailing unit like "4 dm3", a bare digit elsewhere on its
        # own line with no detected image region nearby to rule it out —
        # see _maybe_sequential_qstart) and once for real. Collapse any such
        # run down to its LAST occurrence: the earlier, noise-triggered hit
        # is what cut the question short, so it's never the one worth
        # keeping, and treating the pair as a restart would needlessly
        # truncate the rest of the document at a false alarm.
        deduped: List = []
        i = 0
        while i < len(matches):
            m = matches[i]
            if m.group(1) is not None:
                j = i + 1
                while j < len(matches) and matches[j].group(1) == m.group(1):
                    j += 1
                deduped.append(matches[j - 1])
                i = j
            else:
                deduped.append(m)
                i += 1
        matches = deduped

        # A paper split into separate sections (e.g. "Easy Questions 1, 2,
        # 3...", then "Medium Questions 1, 2, 3...") restarts its numbering
        # instead of counting straight through. Rather than track each
        # section as its own namespace, treat the restart as the end of this
        # extraction pass — stop here and let the next "Extract More" batch
        # pick back up from that page, where the restarted numbering is
        # simply a fresh "1" handled the same as any other paper.
        restart_page = None
        restart_pos = None
        last_num = None
        cutoff = len(matches)
        for idx, m in enumerate(matches):
            if m.group(1) is None:
                continue
            num = int(m.group(1))
            if last_num is not None and num <= last_num:
                restart_page = get_page_for_pos(m.start())
                restart_pos = m.start()
                cutoff = idx
                break
            last_num = num
        matches = matches[:cutoff]
        if not matches:
            return [], restart_page
        # Cap the last retained match's segment at the restart position (not
        # end-of-text) — otherwise, with no next match left in the truncated
        # list to bound it, it would swallow everything after the restart too.
        text_limit = restart_pos if restart_pos is not None else len(text)

        items = []
        path: List[str] = []
        for idx, m in enumerate(matches):
            if m.group(1) is not None:
                path = [m.group(1)]
            elif m.group(2) is not None:
                path = [path[0], m.group(2)]
            else:
                path = path[:2] + [m.group(3)]

            seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else text_limit
            seg_text = re.sub(r"<<<PAGE_\d+>>>", "", text[m.end():seg_end]).strip()
            items.append({"path": tuple(path), "text": seg_text, "page_num": get_page_for_pos(m.start())})

        def is_proper_prefix(a: tuple, b: tuple) -> bool:
            return len(a) < len(b) and b[: len(a)] == a

        leaves = [it for it in items if not any(is_proper_prefix(it["path"], other["path"]) for other in items)]

        merged_chunks = []
        for leaf in leaves:
            leaf_path = leaf["path"]
            ancestors = sorted(
                (it for it in items if it["path"] == leaf_path[: len(it["path"])]),
                key=lambda it: len(it["path"]),
            )
            merged_text = " ".join(it["text"] for it in ancestors if it["text"])
            # A figure referenced by a shared stem (e.g. "(c)" introducing a
            # graph) often sits on an earlier page than the leaf's own
            # "(i)"/"(ii)" marker, once the stem's text is folded in — keep
            # every ancestor's page so image lookup isn't limited to the
            # leaf's own page.
            ancestor_pages = []
            for it in ancestors:
                if it["page_num"] not in ancestor_pages:
                    ancestor_pages.append(it["page_num"])
            merged_chunks.append({
                "question_number": ".".join(leaf_path),
                "text": merged_text,
                "page_num": leaf["page_num"],
                "page_nums": ancestor_pages,
            })
        return merged_chunks, restart_page

    def _postprocess_question_text(self, text: str) -> str:
        # Remove page numbers, headers, footers, and other known extraneous patterns
        patterns = [
            r"Page \d+ of \d+",
            r"Turn over",
            r"Copyright.*",
            r"IB/M/\d+/\w+",
            r"\*\d+\*",
            r"^\s*\d+\s*$",  # Standalone numbers
            r"^\s*\d+\.\d*\s*$",  # Standalone question numbers
        ]
        for pattern in patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)
        return text.strip()

    def _resolve_marker_position(self, q_positions: Dict[str, float], q_num: str):
        """Find q_num's y-position on a page, falling back to the position of
        its longest recorded ancestor (e.g. "1.c" for "1.c.i") when the leaf's
        own marker isn't on this particular page — only the stem that
        introduces a figure is. Returns (label, y0) or (None, None)."""
        if not q_positions:
            return None, None
        if q_num in q_positions:
            return q_num, q_positions[q_num]
        if "." in q_num:
            parts = q_num.split(".")
            for end in range(len(parts) - 1, 0, -1):
                candidate = ".".join(parts[:end])
                if candidate in q_positions:
                    return candidate, q_positions[candidate]
        return None, None

    def _get_images_for_chunk(self, chunk: Dict, page_image_map: Dict, page_question_y: Dict) -> List[str]:
        """Return image paths associated with this question.

        Prefers matching an image to the question by its "Fig X.Y" label
        appearing in the question's own text — the most precise signal, and
        the only one that survives a compiled worksheet reusing the same
        label (e.g. "Fig. 1.1") for unrelated images under different
        questions. Falls back to vertical on-page position when no image on
        the chunk's candidate pages carries a label the text mentions.
        """
        q_num = chunk.get("question_number")
        mentioned_figs = {m.group(1) for m in self._FIG_LABEL_RE.finditer(chunk.get("text") or "")}
        # Bare-numbered leaves (see _segment_bare_numbered_questions) carry
        # every ancestor's page, since a figure a stem introduces can sit on
        # an earlier page than the leaf's own marker.
        pages = chunk.get("page_nums") or [chunk.get("page_num")]
        page_question_y = page_question_y or {}

        # If this question's text names any figure, trust that completely
        # rather than mixing it with the position fallback: a page with no
        # label match of its own would otherwise fall through to "every image
        # on this page", silently letting an unrelated image back in even
        # though a different candidate page matched by label correctly.
        if mentioned_figs:
            label_matches: List[str] = []
            seen_labels = set()
            for page_num in pages:
                for _, path, fig_label in page_image_map.get(page_num, []):
                    if fig_label and fig_label in mentioned_figs and path not in seen_labels:
                        seen_labels.add(path)
                        label_matches.append(path)
            if label_matches:
                return label_matches

        matched: List[str] = []
        seen = set()
        for page_num in pages:
            all_rects = page_image_map.get(page_num, [])  # [(rect, path, fig_label), ...]
            if not all_rects:
                continue

            q_positions = page_question_y.get(page_num, {})
            pos_label, q_y = self._resolve_marker_position(q_positions, q_num)
            if pos_label is not None:
                # Find the y-start of the next marker on this same page
                next_y = min((qy for qy in q_positions.values() if qy > q_y), default=float("inf"))
                candidates = [path for rect, path, _ in all_rects if rect.y0 >= q_y - 30 and rect.y0 < next_y]
            else:
                # No position info on this page — fall back to every image on it
                candidates = [path for _, path, _ in all_rects]

            for path in candidates:
                if path not in seen:
                    seen.add(path)
                    matched.append(path)

        return matched

    def _call_openai_with_retry(self, **kwargs):
        """Call the OpenAI chat completions API, retrying with backoff on rate limits."""
        for attempt in range(MAX_RETRIES):
            try:
                return self.client.chat.completions.create(**kwargs)
            except RateLimitError:
                if attempt == MAX_RETRIES - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(f"Rate limited by OpenAI, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)

    def _structure_with_ai(self, chunks: List[Dict], page_image_map: Dict = None, page_question_y: Dict = None, bubble_lookup: Dict[str, dict] = None) -> List[Dict]:
        logger.info(f"Processing {len(chunks)} chunks with AI...")
        if page_image_map is None:
            page_image_map = {}
        bubble_lookup = bubble_lookup if bubble_lookup is not None else build_bubble_lookup()
        bubble_prompt_list = "\n".join(sorted(bubble_lookup.keys()))

        questions: List[Optional[Dict]] = [None] * len(chunks)
        ai_success_count = 0
        fallback_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as executor:
            future_to_idx = {
                executor.submit(
                    self._process_single_chunk, idx + 1, chunk, page_image_map, page_question_y,
                    bubble_lookup, bubble_prompt_list,
                ): idx
                for idx, chunk in enumerate(chunks)
            }
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                kind, result = future.result()
                questions[idx] = result
                if kind == "ai":
                    ai_success_count += 1
                else:
                    fallback_count += 1

        logger.info(
            f"AI processing complete: {ai_success_count} AI successes, {fallback_count} fallbacks"
        )
        return questions

    def _process_single_chunk(
        self, i: int, chunk: Dict, page_image_map: Dict, page_question_y: Dict,
        bubble_lookup: Dict[str, dict], bubble_prompt_list: str,
    ) -> Tuple[str, Dict]:
        """Process one question chunk with the AI, falling back to regex parsing on failure.
        Returns ("ai", question_data) or ("fallback", fallback_result)."""
        logger.info(f"Processing question {i}...")
        try:
            logger.debug(f"Sending chunk {i} to OpenAI API...")

            system_message = (
                "You are an exam question formatter with deep subject-matter knowledge "
                "across sciences, maths, and humanities. You extract exam questions with "
                "the precision of a careful examiner — never inventing content, never "
                "dropping real options or marks, and never guessing when uncertain."
            )

            bubble_section = ""
            if bubble_prompt_list:
                bubble_section = f"""

BUBBLE SUGGESTION — pick the single closest-matching topic from this list for this question.
Copy one line EXACTLY as shown (including the " :: " separator), or return "" if nothing fits well.
{bubble_prompt_list}"""

            user_message = f"""Extract and format the following exam question text.

QUESTION TEXT:
{chunk['text']}

INSTRUCTIONS:
1. Combine any introductory stem shared across sub-questions (e.g. 01 with 01.1, 01.2, 01.3...) into a single self-contained question — every sub-part must carry the shared context, not just the first.
2. Remove question numbers (e.g. "01.1"), mark allocations (e.g. "[2 marks]", "(3)"), and instructional phrases (e.g. "Tick one box", "Choose answers from the box", "Complete the sentence") from the question text.
3. Do NOT include the text content of tables or graphs in the question.
4. In the "question" field only: use HTML <br> tags for line breaks (do NOT use \\n) and <strong>/<em> where the source shows emphasis (e.g. <em>Rattus rattus</em>). <br>, <strong> and <em> are the ONLY tags allowed — never use <b>, <i>, <span>, <u>, <sub>, <sup> or any other tag, and never put tags around a ____ blank. NEVER use <sub> or <sup> tags anywhere — the app cannot display them. Write subscripts and superscripts as Unicode characters instead, in the question and options alike: chemical formulas, isotopes, ionic charges, units with exponents, and mathematical exponents (e.g. CO₂, Na⁺, SO₄²⁻, m³, cm³, m/s², 10⁻⁸, x²); write degrees as ° (e.g. 25 °C), and fractions inline as a/b (e.g. 3/5, m/s).
5. The "answer" field must always be plain text — never use <br> or any other HTML tag there. For "Short Answer" and "Fill in the Blank", write values the way a student would type them, without Unicode superscripts/subscripts (e.g. "m/s2", not "m/s²").
6. Never use the × or ÷ Unicode symbols anywhere — write "x" for multiplication and "/" for division instead (e.g. "mass number x percentage", not "mass number × percentage"). These symbols are unreliable to generate correctly.
7. If the text contains the literal marker "[table — see attached image]", that table's actual cell content is only visible in the attached image, not in this text — never invent plausible-looking option text to fill the gap. For "Multiple Choice", set "options" to just the row letters actually present (e.g. ["A", "B", "C", "D"]), with no text after each letter, and set "answer" to [] — you cannot determine the correct option without seeing the image.

QUESTION TYPE — choose exactly one:
- "Multiple Choice": a fixed set of selectable options (labelled A/B/C/D or listed items). Extract every option actually present — do not pad to or truncate at 4. Use this type even when more than one option must be selected (e.g. "Tick two boxes").
- "Fill in the Blank": the question sentence itself contains blanks (____) for the student to complete.
- "Short Answer": everything else — calculations, describe/explain, extended/essay responses, true/false statement grids, matching pairs, ordering/sequencing, and diagram labelling.

Within "Short Answer", format the answer to fit the sub-format:
- True/false grid: question lists the statements; answer has one entry per statement, "<statement> — True" or "<statement> — False".
- Matching: question lists both columns; answer has one entry per pair, "<left> → <right>".
- Ordering: answer is the items in correct order.
- Diagram labelling: answer has one entry per label, "<label> — <term>".
- Calculation: include both a unit-bearing entry (e.g. "12 N") and a value-only entry with no units (e.g. "12"), so either form is accepted.
- Extended response: answer is 1–3 acceptable phrasings or key marking points.

CRITICAL — for "Multiple Choice":
- "question" contains ONLY the stem — never any option text.
- Every option goes in "options", copied verbatim as plain text with no HTML tags (Unicode characters like CO₂ are fine) — EXCEPT its leading option label (see next point).
- Strip each option's leading position label before saving it — but only when you can confirm it really is just a label, not part of the answer. Check the whole set together: if option 1 starts with "A", option 2 with "B", option 3 with "C", and so on in strict sequence (in any of these forms: "A", "A.", "A)", "(A)"), that consistent pattern across every option is what confirms they're structural markers, not content — remove all of them (e.g. "A. T with C" → "T with C"). The same applies when the sequence is numeric instead of lettered — "1", "1.", "1)", "(1)" for option 1, "2" for option 2, and so on — strip those the same way (e.g. "1. T with C" → "T with C"). If even one option breaks the sequence (out of order, missing, or the leading letter/number is clearly part of the wording itself), leave every option in that question exactly as written — don't guess option-by-option.
- Each "answer" entry must be a byte-for-byte copy of a chosen option string AFTER label-stripping — the UI matches by exact string equality to highlight the correct option(s). Most questions have exactly one correct option; only include more than one entry when the question explicitly asks for multiple selections.
- Set "multi_select" to true ONLY when the question explicitly requires selecting more than one option (e.g. "Tick two boxes", "Select all that apply", "Choose two answers") — in that case "answer" must have 2+ entries. Otherwise set "multi_select" to false, and pick the single best option rather than hedging with extra "answer" entries.

IMPORTANT — for "Fill in the Blank":
- Preserve all ____ blanks in the question text. "options" must always be [].
- "answer" is every acceptable phrasing/synonym for the blank(s), one entry per variant.

If genuinely uncertain of the answer, return [] rather than guessing.
{bubble_section}

Return JSON with: question, type, options, answer (array of strings), multi_select, suggested_bubble."""

            response = self._call_openai_with_retry(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "exam_question",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": ["Multiple Choice", "Fill in the Blank", "Short Answer"],
                                },
                                "options": {"type": "array", "items": {"type": "string"}},
                                "answer": {"type": "array", "items": {"type": "string"}},
                                "multi_select": {"type": "boolean"},
                                "suggested_bubble": {"type": "string"},
                            },
                            "required": [
                                "question",
                                "type",
                                "options",
                                "answer",
                                "multi_select",
                                "suggested_bubble",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                temperature=0,
                max_tokens=1000,
            )

            logger.debug(f"Received response from OpenAI for question {i}")
            content = response.choices[0].message.content or ""
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
            try:
                result = json.loads(content)
            except Exception as e:
                logger.warning(f"AI output not valid JSON for question {i}: {e}")
                raise

            question_text = self._postprocess_question_text(result.get("question", "").strip())
            question_type = result.get("type", "").strip()

            # Use the original merged chunk text for fill-the-gap detection
            if question_type == "Fill in the Blank" and not self.is_fill_the_gap_question(chunk['text']):
                question_type = "Short Answer"
                logger.debug(f"Overriding AI classification for question {i} - not a true fill-in-the-blank")

            # Remove blanks for non-fill-the-blank questions
            if question_type != "Fill in the Blank":
                question_text = re.sub(r"_+", "", question_text)

            # Validate options — Fill in the Blank never has options
            options = result.get("options", [])
            if not isinstance(options, list) or question_type == "Fill in the Blank":
                options = []

            # Answer is always an array from the model; unwrap single-entry
            # arrays to a plain string, keep arrays for genuine multi-value answers
            answer_list = result.get("answer", [])
            if not isinstance(answer_list, list):
                answer_list = [answer_list] if answer_list else []
            answer = answer_list[0] if len(answer_list) == 1 else answer_list

            # Only trust multi_select when it's actually backed by 2+ answer entries on
            # a Multiple Choice question — guards against a model slip producing a flag
            # that doesn't match the answer shape the mobile app expects.
            multi_select = bool(
                result.get("multi_select", False)
                and question_type == "Multiple Choice"
                and isinstance(answer, list)
                and len(answer) > 1
            )

            # A 3-option Multiple Choice question is usually missing a 4th
            # option lost in extraction — ask the AI for one plausible distractor.
            if question_type == "Multiple Choice" and len(options) == 3:
                try:
                    distractor = self._generate_distractor_option(question_text, options, answer)
                    if distractor and distractor not in options:
                        options.append(distractor)
                        logger.info(f"Question {i}: generated a 4th distractor option")
                except Exception as e:
                    logger.warning(f"Failed to generate distractor option for question {i}: {e}")

            # Resolve the AI's bubble suggestion against the real bubbles reference
            bubble_id = bubble_lookup.get(result.get("suggested_bubble", "").strip())

            page_images = self._get_images_for_chunk(chunk, page_image_map, page_question_y)
            question_data = {
                "_id": str(uuid.uuid4()),
                "question": question_text,
                "options": options,
                "answer": answer,
                "type": question_type,
                "multiSelect": multi_select,
                "image": page_images,
                "bubble_id": bubble_id,
            }

            # Final check: question text must not be empty or just numbers
            if not question_text or re.match(r"^\s*\d+\s*$", question_text):
                logger.warning(f"AI output for question {i} is empty or invalid, using fallback.")
                raise ValueError("Empty or invalid question text")

            logger.info(f"✓ Question {i} processed successfully with AI")
            logger.debug(f"  Final Type: {question_data['type']}")
            logger.debug(f"  Options: {len(question_data['options'])}")
            return ("ai", question_data)

        except Exception as e:
            logger.warning(f"⚠️ AI parsing failed for question {i}: {e}")
            logger.info(f"Using fallback parsing for question {i}...")

            fallback_result = self._fallback_parsing(chunk['text'])
            fallback_result["image"] = self._get_images_for_chunk(chunk, page_image_map, page_question_y)

            logger.info(f"✓ Question {i} processed with fallback method")
            return ("fallback", fallback_result)

    def _generate_distractor_option(self, question_text: str, options: List[str], answer) -> str:
        """Generate one plausible-but-incorrect 4th option for a 3-option MC question."""
        correct = answer if isinstance(answer, str) else (answer[0] if answer else "")
        prompt = f"""This multiple-choice exam question currently has only 3 options. Generate ONE additional plausible-but-incorrect distractor option, styled consistently with the existing options (similar length, tone, and format).

QUESTION: {question_text}
EXISTING OPTIONS: {options}
CORRECT ANSWER: {correct}

Return ONLY the new option text — no labels, no explanation, no quotes."""

        response = self._call_openai_with_retry(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You write plausible but incorrect exam distractor options."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=60,
        )
        return (response.choices[0].message.content or "").strip()

    def _fallback_parsing(self, chunk: str) -> Dict:
        logger.debug("Using fallback parsing method...")

        lines = [line.strip() for line in chunk.strip().splitlines() if line.strip()]
        if not lines:
            logger.debug("Empty chunk, returning default question")
            return {
                "_id": str(uuid.uuid4()),
                "question": "",
                "options": [],
                "answer": "",
                "type": "Short Answer",
                "multiSelect": False,
            }

        full_text = " ".join(lines)

        question_words = full_text.split()
        if question_words and re.match(r"^\d+\.?\s*\d*\.?$", question_words[0]):
            question_words = question_words[1:]

        question_text = " ".join(question_words).strip()

        question_type = self._detect_question_type(question_text)

        logger.debug(f"Fallback result: Type={question_type}")

        return {
            "_id": str(uuid.uuid4()),
            "question": question_text,
            "options": [],
            "answer": "",
            "type": question_type,
            "multiSelect": False,
        }

    def _validate_questions(self, questions: List[Dict]) -> List[Dict]:
        logger.info(f"Validating {len(questions)} questions...")
        validated = []
        validation_errors = 0

        for i, q_data in enumerate(questions, 1):
            try:
                question = Question(**q_data)
                validated.append(question.model_dump())
                logger.debug(f"Question {i} validation: ✓")
            except ValidationError as e:
                validation_errors += 1
                logger.warning(f"Question {i} validation failed: {e}")

                if q_data.get("question"):
                    validated.append(
                        {
                            "_id": q_data.get("_id", str(uuid.uuid4())),
                            "question": q_data.get("question", ""),
                            "options": q_data.get("options", []),
                            "answer": q_data.get("answer", ""),
                            "type": q_data.get("type", "Unknown"),
                            "multiSelect": q_data.get("multiSelect", False),
                            "image": q_data.get("image", []),
                            "bubble_id": q_data.get("bubble_id"),
                        }
                    )
                    logger.debug(f"Question {i} added with fallback validation")

        logger.info(
            f"Validation complete: {len(validated)} questions validated, {validation_errors} validation errors"
        )
        return validated

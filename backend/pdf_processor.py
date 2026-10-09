import re
import fitz  # PyMuPDF for better PDF text extraction
from typing import List, Dict, Optional, Union, Tuple
from pydantic import BaseModel, ValidationError, Field
import json
import os
import time
import logging
import concurrent.futures
from openai import OpenAI, RateLimitError
import uuid

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


def filter_bubbles(subject_id: Optional[str] = None, board_id: Optional[str] = None) -> List[dict]:
    """Narrow the full bubble list to the chosen subject/exam board, via each
    bubble's embedded topic_id.subject_id / topic_id.board_id."""
    def matches(b: dict) -> bool:
        topic = b.get("topic_id") or {}
        if subject_id and topic.get("subject_id") != subject_id:
            return False
        if board_id and topic.get("board_id") != board_id:
            return False
        return True

    return [b for b in ALL_BUBBLES if matches(b)]


def build_bubble_lookup(subject_id: Optional[str] = None, board_id: Optional[str] = None) -> Dict[str, dict]:
    """Build {'<name> :: <topic name>': full bubble object} for the AI to pick from."""
    return {
        f"{b['name']} :: {b.get('topic_id', {}).get('name', '')}": b
        for b in filter_bubbles(subject_id, board_id)
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
        start_page: int = 0, page_count: int = 10,
    ) -> Dict:
        logger.info(
            f"Starting PDF processing for: {filepath} (subject={subject_id}, board={board_id}, "
            f"pages {start_page}-{start_page + page_count - 1})"
        )
        try:
            logger.info("Step 1: Extracting text from PDF...")
            end_page = start_page + page_count
            raw_text, total_pages, page_image_map, page_question_y = self._extract_text(filepath, start_page, end_page)
            actual_end_page = min(end_page, total_pages)
            logger.info(
                f"✓ Text extracted successfully. Pages: {total_pages}, Characters: {len(raw_text)}"
            )

            logger.info("Step 2: Cleaning extracted text...")
            cleaned_text = self._clean_text(raw_text)
            logger.info(
                f"✓ Text cleaned. Characters after cleaning: {len(cleaned_text)}"
            )

            logger.info("Step 3: Segmenting into question chunks...")
            question_chunks = self._segment_questions(cleaned_text)
            logger.info(f"✓ Found {len(question_chunks)} question chunks")

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
            }

            logger.info("✅ PDF processing completed successfully!")
            return result

        except Exception as e:
            logger.error(f"❌ PDF processing failed: {str(e)}")
            raise Exception(f"PDF processing failed: {str(e)}")

    def _extract_text(self, filepath: str, start_page: int = 0, end_page: Optional[int] = None) -> tuple:
        logger.info(f"Opening PDF file: {filepath}")
        doc = fitz.open(filepath)
        total_pages = len(doc)
        end_page = min(end_page, total_pages) if end_page is not None else total_pages
        logger.info(f"PDF opened successfully. Total pages: {total_pages}. Extracting pages {start_page}-{end_page - 1}")

        full_text = ""
        page_image_map = {}   # {page_num: [(fitz.Rect, path), ...]}
        page_question_y = {}  # {page_num: {qnum_str: y0}}

        # Tracks the current dotted path (e.g. ["1", "c"]) for the bare-number
        # format across pages in document order, so a sub-part marker on a
        # later page (e.g. "(i)") can resolve its full path ("1.c.i") even
        # though its top-level number was set on an earlier page.
        bare_path_state = [[]]

        for page_num in range(start_page, end_page):
            page = doc[page_num]
            page_text, bare_q_y = self._build_page_text_with_markers(page, bare_path_state)
            full_text += f"\n<<<PAGE_{page_num}>>>\n" + page_text

            images = self._extract_page_images(doc, page, page_num)
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

        return full_text, total_pages, page_image_map, page_question_y

    # Matches a bold, unpadded question number ("1", "2", "10") opening a
    # line — either alone on its own line, or immediately followed by the
    # question's own text on the same line (layout varies between papers,
    # even within the same document), or a bold "(a)"/"(i)" sub-part label
    # opening a line within a block.
    _BARE_QNUM_RE = re.compile(r"^([1-9]\d?)(?=[ \t]|$)")
    _SUBLETTER_RE = re.compile(r"^\(([a-h])\)")
    _SUBROMAN_RE = re.compile(r"^\(([ivx]{1,4})\)", re.IGNORECASE)

    def _build_page_text_with_markers(self, page, path_state: list) -> Tuple[str, Dict[str, float]]:
        """Reconstruct this page's plain text (equal to page.get_text()) while
        splicing sentinels in front of bold question-number and sub-part markers,
        for papers that number questions with plain integers ('1', '2', ...) and
        letter/roman sub-parts ('(a)', '(i)') instead of CAIE's zero-padded
        '01'/'01.1' scheme. Also returns {dotted_path: y0} for every marker found
        on this page (e.g. "1.c.i"), so images can later be matched by vertical
        position the same way the official format already is.

        Boldness is what separates a real marker from a lookalike that isn't one:
        a stray digit (graph axis label, mark total) or a "(a)/(b)" that's part
        of ordinary prose (e.g. a dichotomous classification key) shares the same
        shape but is set in a regular weight, while the genuine markers are bold.
        Segmentation later falls back to splitting on these sentinels only when
        the official zero-padded pattern finds nothing, so this is a no-op for
        official-format papers beyond leaving harmless sentinels for clean_text
        to strip.

        `path_state` is a mutable single-element list ([[...]]) holding the
        current dotted path, carried across pages in document order by the
        caller so a sub-part marker here can resolve its full path even when
        its top-level number was set on an earlier page.
        """
        out = []
        q_y: Dict[str, float] = {}
        for block in page.get_text("blocks"):
            block_text = block[4]
            lines = block_text.split("\n")
            rect = fitz.Rect(block[:4])
            dict_lines = [
                line for d_block in page.get_text("dict", clip=rect)["blocks"]
                for line in d_block.get("lines", [])
            ]
            non_empty_count = sum(1 for l in lines if l.strip())
            can_check_bold = non_empty_count == len(dict_lines)

            rebuilt_lines = []
            dict_idx = 0
            for line in lines:
                if line.strip():
                    dline = dict_lines[dict_idx] if can_check_bold else None
                    dict_idx += 1
                    line, labels = self._splice_line_marker(line, dline, path_state)
                    for label, y0 in labels:
                        q_y.setdefault(label, y0)
                rebuilt_lines.append(line)
            out.append("\n".join(rebuilt_lines))
        return "".join(out), q_y

    def _splice_line_marker(
        self, line: str, dline: Optional[dict], path_state: list
    ) -> Tuple[str, List[Tuple[str, float]]]:
        if dline is None or not dline["spans"]:
            return line, []
        if "bold" not in dline["spans"][0]["font"].lower():
            return line, []

        y0 = dline["bbox"][1]
        lead_ws = line[: len(line) - len(line.lstrip())]
        rest = line.lstrip()
        labels: List[Tuple[str, float]] = []

        m = self._BARE_QNUM_RE.match(rest.rstrip())
        if m:
            path_state[0] = [m.group(1)]
            labels.append((m.group(1), y0))
            return f"{lead_ws}<<<QSTART_{m.group(1)}>>>{rest}", labels

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
                path_state[0] = path_state[0][:2] + [m.group(1).lower()]
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

    def _extract_page_images(self, doc, page, page_num: int) -> List[tuple]:
        """Return (fitz.Rect, path, fig_label) tuples for all images, tables, and boxes on the page."""
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
        for rect in self._collect_region_rects(page, page_num):
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
            if len(cluster) < 10:  # a handful of stray strokes isn't a graph
                continue
            union = cluster[0]
            for rect in cluster[1:]:
                union |= rect
            # Unlike the bordered-box candidates above, a cluster isn't
            # required to span 20% of the page width — a portrait-oriented
            # illustration (e.g. one species in a multi-species grid) can be
            # well under 120pt wide while still being a complete, legitimate
            # figure. The >=10-stroke requirement already rules out noise;
            # this is just a sanity floor against a stray fleck.
            if union.width < 20 or union.height < 20:
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

        # PyMuPDF table finder — catches tables whose borders are line segments, not rect paths
        try:
            for table in page.find_tables():
                rect = fitz.Rect(table.bbox)
                if not rect.is_empty and rect.width >= page_width * 0.2 and rect.height >= 20:
                    candidates.append(rect)
                    logger.debug(f"Page {page_num}: table found at y={rect.y0:.0f}")
        except AttributeError:
            pass  # find_tables() requires PyMuPDF ≥ 1.23
        except Exception as e:
            logger.warning(f"Table detection failed on page {page_num}: {e}")

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

        # Remove calculation answer placeholders (e.g. "Energy = _____ J")
        # These use "label = blank unit" format and won't match mid-sentence fill blanks
        text = re.sub(r'\s*[A-Za-z\s]+=\s*[\s_]+\s*[A-Za-z]+(?:\s*$|\s*\n)', '', text)
        text = re.sub(r'\s*[A-Za-z\s]+=\s*[\s_]+\s*(?:\s*$|\s*\n)', '', text)
        text = re.sub(r'\s*[A-Za-z\s]+:\s*[\s_]+\s*[A-Za-z]+(?:\s*$|\s*\n)', '', text)
        text = re.sub(r'\s*[A-Za-z\s]+:\s*[\s_]+\s*(?:\s*$|\s*\n)', '', text)

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

    def _segment_questions(self, text: str) -> List[Dict]:
        logger.info("Segmenting text into question chunks...")

        text = re.sub(r"0\s*(\d)\s*\.\s*(\d)", r"0\1.\2", text)
        text = re.sub(r"\b0\s*(\d)(?!\.\d)\b", r"0\1", text)

        question_pattern = r"(0\d(?:\.\d)?[\s\S]*?)(?=\n\s*0\d(?:\.\d)?|\Z)"
        chunks = re.findall(question_pattern, text)

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
            bare_chunks = self._segment_bare_numbered_questions(text)
            if len(bare_chunks) > len(chunks):
                logger.info(
                    f"Found {len(bare_chunks)} question chunks using bare-number fallback "
                    f"(preferred over {len(chunks)} from regex pattern)"
                )
                return bare_chunks
            if not chunks:
                logger.info(f"Found {len(bare_chunks)} question chunks using bare-number fallback")
                return bare_chunks

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
            qnum_match = re.match(r"^(0\d(?:\.\d)?)", current_chunk)
            qnum = qnum_match.group(1) if qnum_match else None

            chunk_pos = text.find(current_chunk[:40], search_start)
            page_num = get_page_for_pos(chunk_pos) if chunk_pos != -1 else 1
            search_start = chunk_pos + 1 if chunk_pos != -1 else search_start

            clean_text = re.sub(
                r"<<<PAGE_\d+>>>|<<<QSTART_\d+>>>|<<<SUBLETTER_[a-h]>>>|<<<SUBROMAN_[a-z]+>>>", "", current_chunk
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
        return merged_chunks

    def _segment_bare_numbered_questions(self, text: str) -> List[Dict]:
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

        marker_re = re.compile(r"<<<QSTART_(\d+)>>>|<<<SUBLETTER_([a-h])>>>|<<<SUBROMAN_([a-z]+)>>>")
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
            return []
        # Drop any sub-part marker before the first surviving top-level number —
        # it can't be meaningfully nested without one.
        first_qstart = next(i for i, m in enumerate(matches) if m.group(1) is not None)
        matches = matches[first_qstart:]

        items = []
        path: List[str] = []
        for idx, m in enumerate(matches):
            if m.group(1) is not None:
                path = [m.group(1)]
            elif m.group(2) is not None:
                path = [path[0], m.group(2)]
            else:
                path = path[:2] + [m.group(3)]

            seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
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
        return merged_chunks

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
4. In the "question" field only: use HTML <br> tags for line breaks (do NOT use \\n), <b>/<i> where the source shows emphasis, and <sub>/<sup> for chemical formulas, isotopes, ionic charges, units with exponents, and mathematical exponents (e.g. CO<sub>2</sub>, Na<sup>+</sup>, m<sup>3</sup>, x<sup>2</sup>).
5. The "answer" field must always be plain text — never use <sub>, <sup>, <br>, or any other HTML tag there, even if the question text uses them for the same value (e.g. write "m/s2", not "m/s<sup>2</sup>").
6. Never use the × or ÷ Unicode symbols anywhere — write "x" for multiplication and "/" for division instead (e.g. "mass number x percentage", not "mass number × percentage"). These symbols are unreliable to generate correctly.

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
- Every option goes in "options", copied verbatim (incl. any HTML tags).
- Each "answer" entry must be a byte-for-byte copy of a chosen option string — the UI matches by exact string equality to highlight the correct option(s). Most questions have exactly one correct option; only include more than one entry when the question explicitly asks for multiple selections.
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

import re
import fitz  # PyMuPDF for better PDF text extraction
from typing import List, Dict
from pydantic import BaseModel, ValidationError, Field
import json
import os
import logging
from openai import OpenAI
import uuid

logging.basicConfig(
    level=logging.DEBUG,  # <- this is the fix
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("pdf_processor.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

class Question(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    question: str
    options: List[str] = []
    answer: str = ""
    type: str = "Multiple Choice"
    image: List[str] = []  # List of image paths associated with the question

class PDFProcessor:
    def __init__(self):
        logger.info("Initializing PDFProcessor...")
        try:
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            logger.info("OpenAI client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}")
            raise

    def process_pdf(self, filepath: str) -> Dict:
        logger.info(f"Starting PDF processing for: {filepath}")
        try:
            logger.info("Step 1: Extracting text from PDF...")
            raw_text, total_pages, page_image_map, page_question_y = self._extract_text(filepath)
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

            logger.info("Step 4: Processing questions with AI...")
            questions = self._structure_with_ai(question_chunks, page_image_map, page_question_y)
            logger.info(f"✓ Processed {len(questions)} questions")

            logger.info("Step 5: Validating question data...")
            validated_questions = self._validate_questions(questions)
            logger.info(f"✓ Validated {len(validated_questions)} questions")

            result = {
                "questions": validated_questions,
                "metadata": {
                    "total_pages": total_pages,
                    "total_questions": len(validated_questions),
                },
            }

            logger.info("✅ PDF processing completed successfully!")
            return result

        except Exception as e:
            logger.error(f"❌ PDF processing failed: {str(e)}")
            raise Exception(f"PDF processing failed: {str(e)}")

    def _extract_text(self, filepath: str) -> tuple:
        logger.info(f"Opening PDF file: {filepath}")
        doc = fitz.open(filepath)
        total_pages = len(doc)
        logger.info(f"PDF opened successfully. Total pages: {total_pages}")

        full_text = ""
        page_image_map = {}   # {page_num: [(fitz.Rect, path), ...]}
        page_question_y = {}  # {page_num: {qnum_str: y0}}

        for page_num in range(0, min(total_pages, 9)):
            page = doc[page_num]
            page_text = page.get_text()
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
            if q_y:
                page_question_y[page_num] = q_y

            logger.debug(f"Extracted text from page {page_num}: {len(page_text)} characters")

        logger.info(f"Text extraction complete. Total characters: {len(full_text)}")
        doc.close()
        logger.debug("PDF document closed")

        return full_text, total_pages, page_image_map, page_question_y

    def _extract_page_images(self, doc, page, page_num: int) -> List[tuple]:
        """Return (fitz.Rect, path) pairs for all images, tables, and boxes on the page."""
        images_dir = os.path.join(os.path.dirname(__file__), "images")
        os.makedirs(images_dir, exist_ok=True)
        saved = []
        seen_rects = []

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
                if pix.colorspace and pix.colorspace.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                filename = f"{uuid.uuid4().hex}.png"
                pix.save(os.path.join(images_dir, filename))
                rect = xref_to_rect.get(xref, fitz.Rect(0, 0, page.rect.width, page.rect.height))
                seen_rects.append(rect)
                saved.append((rect, f"/api/images/{filename}"))
            except Exception as e:
                logger.warning(f"Failed to extract image from page {page_num}: {e}")

        # Vector regions: word banks, tables, bordered diagrams
        for rect in self._collect_region_rects(page, page_num):
            # Deduplicate against raster images and previously saved vector regions
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
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect)
            filename = f"{uuid.uuid4().hex}.png"
            pix.save(os.path.join(images_dir, filename))
            saved.append((rect, f"/api/images/{filename}"))
            logger.debug(f"Page {page_num}: vector region {rect.width:.0f}×{rect.height:.0f}pt saved")

        return saved

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

        merged_chunks = []
        skip_next = False
        # Track cumulative offset to find each chunk in original text
        search_start = 0

        for i in range(len(chunks)):
            if skip_next:
                skip_next = False
                continue

            current_chunk = chunks[i].strip()
            current_qnum_match = re.match(r"^(0\d(?:\.\d)?)", current_chunk)
            current_qnum = current_qnum_match.group(1) if current_qnum_match else None

            # Find position of this chunk in the full text to determine its page
            chunk_pos = text.find(current_chunk[:40], search_start)
            page_num = get_page_for_pos(chunk_pos) if chunk_pos != -1 else 1
            search_start = chunk_pos + 1 if chunk_pos != -1 else search_start

            # Strip page markers from chunk text
            clean_text = re.sub(r"<<<PAGE_\d+>>>", "", current_chunk).strip()

            if i + 1 < len(chunks):
                next_chunk = chunks[i + 1].strip()
                next_qnum_match = re.match(r"^(0\d(?:\.\d)?)", next_chunk)
                next_qnum = next_qnum_match.group(1) if next_qnum_match else None

                if current_qnum and next_qnum:
                    base_current = current_qnum.split(".")[0]
                    base_next = next_qnum.split(".")[0]

                    if (base_current == base_next) and (next_qnum.endswith(".1")):
                        clean_next = re.sub(r"<<<PAGE_\d+>>>", "", next_chunk).strip()
                        merged_text = f"{clean_text} {clean_next}"
                        merged_chunks.append(
                            {"question_number": base_current, "text": merged_text, "page_num": page_num}
                        )
                        skip_next = True
                        continue

            merged_chunks.append(
                {"question_number": current_qnum, "text": clean_text, "page_num": page_num}
            )

        logger.info(f"After merging, total question chunks: {len(merged_chunks)}")
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

    def _get_images_for_chunk(self, chunk: Dict, page_image_map: Dict, page_question_y: Dict) -> List[str]:
        """Return image paths whose on-page position falls within this question's vertical range."""
        page_num = chunk.get("page_num")
        q_num = chunk.get("question_number")
        all_rects = page_image_map.get(page_num, [])  # [(rect, path), ...]
        if not all_rects:
            return []

        q_positions = (page_question_y or {}).get(page_num, {})
        if q_positions and q_num in q_positions:
            q_y = q_positions[q_num]
            # Find the y-start of the next question on the same page
            next_y = float("inf")
            for qn, qy in q_positions.items():
                if qy > q_y:
                    next_y = min(next_y, qy)
            return [
                path for rect, path in all_rects
                if rect.y0 >= q_y - 30 and rect.y0 < next_y
            ]

        # No position info available — return all images on the page
        return [path for _, path in all_rects]

    def _structure_with_ai(self, chunks: List[Dict], page_image_map: Dict = None, page_question_y: Dict = None) -> List[Dict]:
        logger.info(f"Processing {len(chunks)} chunks with AI...")
        if page_image_map is None:
            page_image_map = {}
        questions = []
        ai_success_count = 0
        fallback_count = 0

        for i, chunk in enumerate(chunks, 1):
            logger.info(f"Processing question {i}/{len(chunks)}...")
            try:
                logger.debug(f"Sending chunk {i} to OpenAI API...")

                system_message = (
                    "You are an exam question formatter. "
                    "You always respond with valid JSON only, no markdown, no explanation."
                )

                user_message = f"""Extract and format the following exam question text.

QUESTION TEXT:
{chunk['text']}

INSTRUCTIONS:
1. Combine any introductory stem (e.g. from question 01) with sub-question text (e.g. 01.1) into a single question.
2. Remove question numbers (e.g. "01.1"), mark allocations (e.g. "[2 marks]"), and instructional phrases (e.g. "Tick one box", "Choose answers from the box", "Complete the sentence").
3. Remove answer placeholders for calculation questions (e.g. "Answer = ______ J", "Value: ______").
4. Do NOT include the text content of tables or graphs in the question.
5. Use HTML <br> tags for line breaks. Do NOT use \\n.
6. Use <b> and <i> tags where appropriate.

QUESTION TYPE — choose exactly one:
- "Multiple Choice": question has a fixed set of selectable options (labelled A/B/C/D or listed items to choose from). Extract exactly 4 options; if there are more than 4, drop the least relevant wrong ones; if there are fewer than 4, use only those available.
- "Fill in the Blank": the question sentence itself contains blanks (____) for the student to complete.
- "Short Answer": everything else, including calculations, describe/explain questions, and extended writing.

CRITICAL — for "Multiple Choice" questions:
- The "question" field must contain ONLY the question stem — never include any of the answer options in the question text.
- All selectable options go ONLY in the "options" array.

IMPORTANT — for "Fill in the Blank" questions:
- Preserve all ____ blanks in the question text.
- The "options" field must always be [] — word bank items are captured separately as images.

ANSWER — provide the correct answer in the "answer" field using your own knowledge:
- Multiple Choice: the exact text of the correct option.
- Fill in the Blank: the correct word(s) for each blank, comma-separated if multiple blanks.
- Short Answer: a concise model answer (1–3 sentences).
- If genuinely uncertain, return "".

Return ONLY this JSON:
{{
    "question": "...",
    "type": "Multiple Choice" | "Fill in the Blank" | "Short Answer",
    "options": ["option1", "option2", "option3", "option4"] | [],
    "answer": "..."
}}"""

                response = self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": user_message},
                    ],
                    response_format={"type": "json_object"},
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

                page_images = self._get_images_for_chunk(chunk, page_image_map, page_question_y)
                question_data = {
                    "_id": str(uuid.uuid4()),
                    "question": question_text,
                    "options": options,
                    "answer": result.get("answer", ""),
                    "marks": result.get("marks", ""),
                    "type": question_type,
                    "image": page_images,
                }

                # Final check: question text must not be empty or just numbers
                if not question_text or re.match(r"^\s*\d+\s*$", question_text):
                    logger.warning(f"AI output for question {i} is empty or invalid, using fallback.")
                    raise ValueError("Empty or invalid question text")

                questions.append(question_data)
                ai_success_count += 1

                logger.info(f"✓ Question {i} processed successfully with AI")
                logger.debug(f"  Final Type: {question_data['type']}")
                logger.debug(f"  Options: {len(question_data['options'])}")

            except Exception as e:
                logger.warning(f"⚠️ AI parsing failed for question {i}: {e}")
                logger.info(f"Using fallback parsing for question {i}...")

                fallback_result = self._fallback_parsing(chunk['text'])
                fallback_result["image"] = self._get_images_for_chunk(chunk, page_image_map, page_question_y)
                questions.append(fallback_result)
                fallback_count += 1

                logger.info(f"✓ Question {i} processed with fallback method")

        logger.info(
            f"AI processing complete: {ai_success_count} AI successes, {fallback_count} fallbacks"
        )
        return questions

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
                            "question": q_data.get("question", ""),
                            "options": q_data.get("options", []),
                            "answer": q_data.get("answer", ""),
                            "type": q_data.get("type", "Unknown"),
                        }
                    )
                    logger.debug(f"Question {i} added with fallback validation")

        logger.info(
            f"Validation complete: {len(validated)} questions validated, {validation_errors} validation errors"
        )
        return validated

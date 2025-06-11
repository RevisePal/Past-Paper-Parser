import re
import fitz  # PyMuPDF for better PDF text extraction
from typing import List, Dict
from pydantic import BaseModel, ValidationError
import json
import openai
import os
import logging
from openai import OpenAI

logging.basicConfig(
    level=logging.DEBUG,  # <- this is the fix
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("pdf_processor.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


class Question(BaseModel):
    question: str
    options: List[str] = []
    correct_answer: str = ""
    marks: str = ""
    type: str = "Multiple Choice"


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
            raw_text, total_pages = self._extract_text(filepath)
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
            questions = self._structure_with_ai(question_chunks)
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
        for page_num in range(1, 5):
            page = doc[page_num]
            page_text = page.get_text()
            full_text += page_text
            logger.debug(
                f"Extracted text from page {page_num}: {len(page_text)} characters"
            )

        logger.info(f"Text extraction complete. Total characters: {len(full_text)}")
        doc.close()
        logger.debug("PDF document closed")

        return full_text, total_pages

    def _clean_text(self, text: str) -> str:
        logger.info("Starting text cleaning process...")
        original_length = len(text)

        text = text.replace("\u00a0", " ")
        text = re.sub(r'[✓]|\(\)', '', text)
        text = re.sub(r"Figure\s*\d+", "the diagram below", text, flags=re.IGNORECASE)
        text = re.sub(r"\bTable\s*\d+", "the table below", text, flags=re.IGNORECASE)
        logger.debug("Replaced non-breaking spaces")
        text = self._preserve_fill_in_blanks(text)

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
        logger.debug("Preserving fill-in-the-blank patterns...")
        
        # First check if this is actually a fill-the-gap question
        if not self.is_fill_the_gap_question(text):
            # If not, clean ALL underscores (they're not blanks)
            text = re.sub(r"_+", "", text)
            return text
        
        # Only process underscores if it's a fill-the-gap question
        # Replace sequences of underscores with consistent blanks
        text = re.sub(r"_{3,}", "____", text)
        
        # Handle spaced underscores like "_ _ _ _"
        text = re.sub(r"(?:_\s+){2,}_?", "____", text)
        
        # Handle single underscores in specific contexts
        text = re.sub(r"(?<=\s)_(?=\s)", "____", text)      # Between spaces
        text = re.sub(r"(?<=\s)_(?=[.,;:])", "____", text)  # Before punctuation
        text = re.sub(r"(?<=[.,;:])\s*_(?=\s)", "____", text)  # After punctuation
        
        logger.debug("Fill-in-the-blank preservation complete")
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

        merged_chunks = []
        skip_next = False

        for i in range(len(chunks)):
            if skip_next:
                skip_next = False
                continue

            current_chunk = chunks[i].strip()
            current_qnum_match = re.match(r"^(0\d(?:\.\d)?)", current_chunk)
            current_qnum = current_qnum_match.group(1) if current_qnum_match else None

            if i + 1 < len(chunks):
                next_chunk = chunks[i + 1].strip()
                next_qnum_match = re.match(r"^(0\d(?:\.\d)?)", next_chunk)
                next_qnum = next_qnum_match.group(1) if next_qnum_match else None

                if current_qnum and next_qnum:
                    base_current = current_qnum.split(".")[0]
                    base_next = next_qnum.split(".")[0]

                    if (base_current == base_next) and (next_qnum.endswith(".1")):
                        merged_text = f"{current_chunk} {next_chunk}"
                        merged_chunks.append(
                            {"question_number": base_current, "text": merged_text}
                        )
                        skip_next = True
                        continue

            merged_chunks.append(
                {"question_number": current_qnum, "text": current_chunk}
            )

        logger.info(f"After merging, total question chunks: {len(merged_chunks)}")
        return merged_chunks

    def _structure_with_ai(self, chunks: List[Dict]) -> List[Dict]:
        logger.info(f"Processing {len(chunks)} chunks with AI...")
        questions = []
        ai_success_count = 0
        fallback_count = 0

        for i, chunk in enumerate(chunks, 1):
            logger.info(f"Processing question {i}/{len(chunks)}...")

            try:
                logger.debug(f"Sending chunk {i} to OpenAI API...")

                prompt = f"""
    You are a formatter for exam questions.

    Analyze this exam question text and extract the information. Be very careful to:
    1. Always add the text extracted from an integer number e.g 01 with the text of the question after it e.g 01.1 to make one single question
    2. Clean the question text (remove question numbers like "01.1", but keep any reference like "the image below")
    3. For fill-in-the-blank questions, preserve all underscores that represent blanks (____)
    4. Multiple choice options MUST NOT be included in the question text.
    5. Do NOT include the content of tables or graphs in the question text.
    6. Use HTML <br> tags for new lines — **do NOT use \\n**. Add <br> wherever a line break would improve clarity or match the source formatting.

    Now process the following text: {chunk}

    Return ONLY valid JSON in this exact format:
    {{
        "question": "clean question text without numbers but preserving blanks (____)",
        "type": "Fill in the Blank" or "Multiple Choice" or "Short Answer",
        "options": ["option1", "option2", "option3"] or [],
    }}
    """

                response = self.client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_tokens=500,
                )

                logger.debug(f"Received response from OpenAI for question {i}")
                result = json.loads(response.choices[0].message.content)
                logger.debug("🔍 AI FORMATTED OUTPUT:")
                logger.debug(json.dumps(result, indent=2))

                # === STRICT VALIDATION ADDED HERE ===
                question_text = result.get("question", "").strip()
                question_type = result.get("type", "").strip()
                
                # Override AI if it incorrectly labeled as fill-in-the-blank
                if question_type == "Fill in the Blank" and not self.is_fill_the_gap_question(question_text):
                    question_type = "Short Answer"
                    logger.debug(f"Overriding AI classification for question {i} - not a true fill-in-the-blank")

                # Remove blanks for non-fill-the-blank questions
                if question_type != "Fill in the Blank":
                    question_text = re.sub(r"_+", "", question_text)

                question_data = {
                    "question": question_text,
                    "options": result.get("options", []),
                    "correct_answer": "",
                    "marks": result.get("marks", ""),
                    "type": question_type,
                }

                questions.append(question_data)
                ai_success_count += 1

                logger.info(f"✓ Question {i} processed successfully with AI")
                logger.debug(f"  Final Type: {question_data['type']}")
                logger.debug(f"  Options: {len(question_data['options'])}")

            except Exception as e:
                logger.warning(f"⚠️ AI parsing failed for question {i}: {e}")
                logger.info(f"Using fallback parsing for question {i}...")

                fallback_result = self._fallback_parsing(chunk)
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
                "question": "",
                "options": [],
                "correct_answer": "",
                "marks": "",
                "type": "Short Answer",
            }

        full_text = " ".join(lines)

        question_words = full_text.split()
        if question_words and re.match(r"^\d+\.?\s*\d*\.?$", question_words[0]):
            question_words = question_words[1:]

        question_text = " ".join(question_words).strip()

        question_type = self._detect_question_type(question_text)

        logger.debug(f"Fallback result: Type={question_type}, Marks={marks}")

        return {
            "question": question_text,
            "options": [],
            "correct_answer": "",
            "type": question_type,
        }

    def _validate_questions(self, questions: List[Dict]) -> List[Dict]:
        logger.info(f"Validating {len(questions)} questions...")
        validated = []
        validation_errors = 0

        for i, q_data in enumerate(questions, 1):
            try:
                question = Question(**q_data)
                validated.append(question.dict())
                logger.debug(f"Question {i} validation: ✓")
            except ValidationError as e:
                validation_errors += 1
                logger.warning(f"Question {i} validation failed: {e}")

                if q_data.get("question"):
                    validated.append(
                        {
                            "question": q_data.get("question", ""),
                            "options": q_data.get("options", []),
                            "correct_answer": q_data.get("correct_answer", ""),
                            "type": q_data.get("type", "Unknown"),
                        }
                    )
                    logger.debug(f"Question {i} added with fallback validation")

        logger.info(
            f"Validation complete: {len(validated)} questions validated, {validation_errors} validation errors"
        )
        return validated

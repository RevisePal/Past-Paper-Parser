import re
import json
import os
from typing import List, Dict, Any
from pydantic import BaseModel, ValidationError
from dotenv import load_dotenv

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

load_dotenv()


class Question(BaseModel):
    question_number: str = ""
    question: str
    options: List[str] = []
    correct_answer: str = ""
    marks: str = ""
    type: str = "Multiple Choice"


class PDFProcessor:
    def __init__(self):
        self.openai_client = None
        if OpenAI and os.getenv("OPENAI_API_KEY"):
            self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def process_pdf(self, filepath: str) -> Dict[str, Any]:
        try:
            raw_text, total_pages, extraction_method = self._extract_text(filepath)

            cleaned_text = self._clean_text(raw_text)

            question_chunks = self._segment_questions(cleaned_text)

            questions = []
            if self.openai_client and question_chunks:
                questions = self._structure_with_llm(question_chunks)
            else:
                questions = self._structure_without_llm(question_chunks)

            validated_questions = self._validate_questions(questions)

            return {
                "questions": validated_questions,
                "metadata": {
                    "total_pages": total_pages,
                    "extraction_method": extraction_method,
                    "total_questions": len(validated_questions),
                },
            }

        except Exception as e:
            raise Exception(f"PDF processing failed: {str(e)}")

    def _extract_text(self, filepath: str) -> tuple:
        if not pdfplumber:
            return "", 0, "pdfplumber not available"

        try:
            with pdfplumber.open(filepath) as pdf:
                text_parts = []
                total_pages = len(pdf.pages)

                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

                if text_parts:
                    return "\n".join(text_parts), total_pages, "pdfplumber"
                else:
                    return "", total_pages, "No text found in PDF"

        except Exception as e:
            return "", 0, f"Extraction failed: {str(e)}"

    def _clean_text(self, text: str) -> str:
        text = re.sub(r"Page \d+.*?\n", "", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<=\w)\s+(?=\w)", "", text)
        text = re.sub(r"\n+", "\n", text)
        text = re.sub(r"\s+", " ", text)

        lines = text.split("\n")
        cleaned_lines = []

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            if (
                i < len(lines) - 1
                and not line.endswith(".")
                and not line.endswith("?")
                and not line.endswith(":")
                and not re.match(r"^\d+\.", lines[i + 1].strip())
            ):
                line += " "

            cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    def _segment_questions(self, text: str) -> List[str]:
        question_pattern = r"(?=\b\d+\s*\.\s*)"
        chunks = re.split(question_pattern, text)

        question_chunks = []
        for chunk in chunks:
            chunk = chunk.strip()
            if chunk and re.match(r"^\d+\s*\.", chunk):
                question_chunks.append(chunk)

        return question_chunks

    def _structure_with_llm(self, chunks: List[str]) -> List[Dict[str, Any]]:
        questions = []

        system_prompt = """
        You are an expert at extracting structured question data from exam papers.
        Extract each question into JSON format with these fields:
        - question_number: The question number (e.g., "1", "2a", "3.1")
        - question: The full question text
        - options: Array of multiple choice options (if any)
        - correct_answer: The correct answer (if provided)
        - marks: Point value or marks (if mentioned)
        - type: "Multiple Choice", "Short Answer", "Essay", etc.
        
        Return only valid JSON. If no clear question is found, return empty object {}.
        """

        for chunk in chunks[:10]:
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": f"Extract question data from: {chunk}",
                        },
                    ],
                    temperature=0.1,
                )

                result = response.choices[0].message.content.strip()
                if result.startswith("{") and result.endswith("}"):
                    question_data = json.loads(result)
                    if question_data.get("question"):
                        questions.append(question_data)

            except Exception as e:
                print(f"LLM processing error: {e}")
                continue

        return questions

    def _structure_without_llm(self, chunks: List[str]) -> List[Dict[str, Any]]:
        questions = []

        for chunk in chunks:
            question_match = re.match(
                r"^(\d+(?:\.\d+)?[a-z]?)\s*\.\s*(.*)", chunk, re.DOTALL
            )
            if not question_match:
                continue

            question_number = question_match.group(1)
            content = question_match.group(2).strip()

            options = []
            option_pattern = r"[A-E]\)\s*([^\n]+)"
            option_matches = re.findall(option_pattern, content)
            if option_matches:
                options = [match.strip() for match in option_matches]
                content = re.sub(option_pattern, "", content).strip()

            marks_match = re.search(r"\[(\d+)\s*marks?\]", content, re.IGNORECASE)
            marks = marks_match.group(1) if marks_match else ""

            question_type = "Multiple Choice" if options else "Short Answer"

            questions.append(
                {
                    "question_number": question_number,
                    "question": content,
                    "options": options,
                    "correct_answer": "",
                    "marks": marks,
                    "type": question_type,
                }
            )

        return questions

    def _validate_questions(
        self, questions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        validated = []

        for q_data in questions:
            try:
                question = Question(**q_data)
                validated.append(question.dict())
            except ValidationError:
                if q_data.get("question"):
                    validated.append(
                        {
                            "question_number": q_data.get("question_number", ""),
                            "question": q_data.get("question", ""),
                            "options": q_data.get("options", []),
                            "correct_answer": q_data.get("correct_answer", ""),
                            "marks": q_data.get("marks", ""),
                            "type": q_data.get("type", "Unknown"),
                        }
                    )

        return validated

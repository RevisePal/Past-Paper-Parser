import re
import fitz  # PyMuPDF for better PDF text extraction
from typing import List, Dict
from pydantic import BaseModel, ValidationError


class Question(BaseModel):
    question: str
    options: List[str] = []
    correct_answer: str = ""
    marks: str = ""
    type: str = "Multiple Choice"


class PDFProcessor:
    def process_pdf(self, filepath: str) -> Dict:
        try:
            raw_text, total_pages = self._extract_text(filepath)
            cleaned_text = self._clean_text(raw_text)
            question_chunks = self._segment_questions(cleaned_text)
            questions = self._structure_without_llm(question_chunks)
            validated_questions = self._validate_questions(questions)

            return {
                "questions": validated_questions,
                "metadata": {
                    "total_pages": total_pages,
                    "total_questions": len(validated_questions),
                },
            }
        except Exception as e:
            raise Exception(f"PDF processing failed: {str(e)}")

    def _extract_text(self, filepath: str) -> tuple:
        doc = fitz.open(filepath)
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        return full_text, len(doc)

    def _clean_text(self, text: str) -> str:
        text = re.sub(r"Tick\s*\([✓✔]?\)\s*one box", "[TICK_ONE_BOX]", text, flags=re.IGNORECASE)
        text = re.sub(r"Tick\s*\([✓✔]?\)\s*two boxes", "[TICK_TWO_BOXES]", text, flags=re.IGNORECASE)
        text = re.sub(r"[✅✓✔✗□■▪•◦]", "", text)
        text = text.replace("\u00a0", " ")
        text = re.sub(r"\[\s*\d+\s*mark[s]?\s*\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\(\)", "", text)

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
        for pattern in patterns_to_remove:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        text = re.sub(r"\n\s*\d+\s*\n", "\n", text)
        text = re.sub(r"\n{2,}", "\n", text)
        text = re.sub(r"[ ]{2,}", " ", text)

        return text.strip()

    def _segment_questions(self, text: str) -> List[str]:
        question_pattern = r"(0\s*\d\s*\.\s*\d[\s\S]*?)(?=\n\s*0\s*\d\s*\.\s*\d|\Z)"
        return re.findall(question_pattern, text)

    def _structure_without_llm(self, chunks: List[str]) -> List[Dict]:
        def is_figure_or_diagram_line(line: str) -> bool:
            # Exclude lines like 'Figure 2', lines with only symbols, or very short lines
            if re.match(r"^Figure\\s*\\d+", line, re.IGNORECASE):
                return True
            if re.match(r"^[+\-|=~_]+$", line.strip()):
                return True
            if re.match(r"^\s*$", line):
                return True
            if len(line.strip()) < 2 and not line.strip().isalnum():
                return True
            return False

        questions = []
        for chunk in chunks:
            lines = [line.strip() for line in chunk.strip().splitlines() if line.strip()]
            if not lines:
                continue

            tick_marker_idx = None
            tick_type = None
            for idx, line in enumerate(lines):
                if "[TICK_ONE_BOX]" in line or "[TICK_TWO_BOXES]" in line:
                    tick_marker_idx = idx
                    tick_type = "Multiple Choice"
                    break

            if tick_marker_idx is not None:
                # Question is everything before the marker (excluding question number and figure/diagram lines)
                question_lines = [l for l in lines[1:tick_marker_idx] if not is_figure_or_diagram_line(l)]
                # Options are everything after the marker
                options = [l for l in lines[tick_marker_idx+1:] if l]
            else:
                question_lines = [l for l in lines[1:] if not is_figure_or_diagram_line(l)]
                options = []

            question_text = " ".join(question_lines).strip()

            marks_match = re.search(r"\[(\d+) mark", question_text)
            marks = marks_match.group(1) if marks_match else ""

            question_type = tick_type if tick_type else ("Multiple Choice" if options else "Short Answer")

            questions.append({
                "question": question_text,
                "options": options,
                "correct_answer": "",
                "marks": marks,
                "type": question_type,
            })

        return questions

    def _validate_questions(self, questions: List[Dict]) -> List[Dict]:
        validated = []
        for q_data in questions:
            try:
                question = Question(**q_data)
                validated.append(question.dict())
            except ValidationError:
                if q_data.get("question"):
                    validated.append({
                        "question": q_data.get("question", ""),
                        "options": q_data.get("options", []),
                        "correct_answer": q_data.get("correct_answer", ""),
                        "marks": q_data.get("marks", ""),
                        "type": q_data.get("type", "Unknown"),
                    })
        return validated

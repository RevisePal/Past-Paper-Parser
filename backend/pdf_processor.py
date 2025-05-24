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
        # First replace specific patterns we want to keep with placeholders
        text = re.sub(r"Tick\s*\([✓✔]\)\s*one box", "[TICK_ONE_BOX]", text)
        
        # Remove all symbol variants
        text = re.sub(r"[✅✓✔✗□■▪•◦]", "", text)
        
        # Replace non-breaking space
        text = text.replace("\u00a0", " ")

        # Remove [1 mark], [2 marks], etc.
        text = re.sub(r"\[\s*\d+\s*mark[s]?\s*\]", "", text, flags=re.IGNORECASE)

        # Remove tick/cross/box symbols and ()
        text = re.sub(r"\(\)", "", text)

        # Remove embedded phrases or line noise
        patterns_to_remove = [
            r"Do not write outside the box",
            r"Turn over",
            r"IB/M/\d+/\w+",
            r"IB/M/\w+",
            r"Copyright .*?\n?",
            r"\*\d+\*",
        ]
        for pattern in patterns_to_remove:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        # Remove standalone numbers (like page numbers)
        text = re.sub(r"\n\s*\d+\s*\n", "\n", text)

        # Remove excessive newlines or spaces
        text = re.sub(r"\n{2,}", "\n", text)
        text = re.sub(r"[ ]{2,}", " ", text)

        return text.strip()

    def _segment_questions(self, text: str) -> List[str]:
        # Match question numbers like 01.1, 02.3 etc. and capture until next question or end of text
        question_pattern = r"(0\s*\d\s*\.\s*\d[\s\S]*?)(?=\n\s*0\s*\d\s*\.\s*\d|\Z)"
        return re.findall(question_pattern, text)

    def _structure_without_llm(self, chunks: List[str]) -> List[Dict]:
        questions = []
        option_prefixes = ("A)", "B)", "C)", "D)", "E)", "A.", "B.", "C.", "D.", "E.")

        for chunk in chunks:
            lines = [line.strip() for line in chunk.strip().splitlines() if line.strip()]
            if not lines:
                continue

            # Handle "Tick one box" style questions differently
            if "[TICK_ONE_BOX]" in chunk:
                question_lines = []
                options = []
                
                # Extract the actual question (before the tick instruction)
                for line in lines[1:]:  # Skip question number
                    if "[TICK_ONE_BOX]" in line:
                        break
                    question_lines.append(line)
                    
                # Options come after the tick instruction
                tick_pos = chunk.find("[TICK_ONE_BOX]")
                option_lines = [l.strip() for l in chunk[tick_pos:].splitlines() 
                              if any(l.strip().startswith(p) for p in option_prefixes)]
                
                for line in option_lines:
                    option_text = re.sub(r"^[A-E][\)\.]\s*", "", line)
                    options.append(option_text)
                    
                question_text = " ".join(question_lines).strip()
                
                questions.append({
                    "question": question_text,
                    "options": options,
                    "correct_answer": "",
                    "marks": "1",  # Default for tick-box questions
                    "type": "Multiple Choice",
                })
            else:
                # Original processing for other question types
                question_lines = []
                options = []

                # Skip the question number line (usually the first line)
                for line in lines[1:]:
                    if any(line.startswith(prefix) for prefix in option_prefixes):
                        # Extract option text after prefix
                        option_text = re.sub(r"^[A-E][\)\.]\s*", "", line)
                        options.append(option_text)
                    else:
                        question_lines.append(line)

                question_text = " ".join(question_lines).strip()

                marks_match = re.search(r"\[(\d+) mark", question_text)
                marks = marks_match.group(1) if marks_match else ""

                question_type = "Multiple Choice" if options else "Short Answer"

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
# Past Paper Parser

A full-stack application that extracts structured questions from PDF exam papers using React frontend and Python backend.

## Features

- **PDF Upload**: Drag-and-drop interface for PDF files
- **Text Extraction**: Uses pdfplumber for text-based PDFs and OCR for scanned documents
- **Question Parsing**: Automatically segments and structures questions
- **LLM Integration**: Optional OpenAI integration for enhanced question extraction
- **Modern UI**: Beautiful, responsive React interface
- **JSON Export**: Download extracted questions as structured JSON

## Architecture

### Frontend (React)

- Modern React application with hooks
- Drag-and-drop file upload
- Real-time processing feedback
- Interactive question display
- JSON download functionality

### Backend (Python/Flask)

- Flask API with CORS support
- Multi-stage PDF processing pipeline:
  1. **PDF Ingestion** - Extract text using pdfplumber or OCR
  2. **Text Cleaning** - Remove headers, normalize spacing
  3. **Question Segmentation** - Split into logical question chunks
  4. **LLM Structuring** - Optional OpenAI processing for better accuracy
  5. **Validation** - Ensure data integrity
  6. **Output** - Return structured JSON

## Setup

### Prerequisites

- Node.js 16+ and npm
- Python 3.8+
- (Optional) OpenAI API key for enhanced processing

### Frontend Setup

```bash
npm install
npm start
```

### Backend Setup

```bash
cd backend
pip install -r ../requirements.txt
python app.py
```

### Environment Configuration

Create a `.env` file in the backend directory:

```
OPENAI_API_KEY=your_openai_api_key_here
```

## Usage

1. Start both frontend and backend servers
2. Open http://localhost:3000 in your browser
3. Upload a PDF exam paper using drag-and-drop
4. Wait for processing to complete
5. View extracted questions in the interface
6. Download results as JSON if needed

## Processing Pipeline

The application follows a 6-stage processing pipeline:

1. **Ingest PDF** - Extract raw text or images from PDF
2. **Clean Text** - Remove artifacts and normalize formatting
3. **Segment Questions** - Split text into question chunks
4. **Structure with LLM** - Use AI to extract structured data (optional)
5. **Validate** - Ensure data integrity and completeness
6. **Output** - Return structured JSON with questions and metadata

## API Endpoints

- `POST /api/process-pdf` - Upload and process PDF file
- `GET /api/health` - Health check endpoint

## Dependencies

### Frontend

- React 18
- react-dropzone for file uploads
- axios for API calls

### Backend

- Flask for web framework
- pdfplumber for PDF text extraction
- pytesseract for OCR (optional)
- OpenAI for enhanced processing (optional)
- Pydantic for data validation

## File Structure

```
Past-Paper-Parser/
├── src/                    # React frontend
│   ├── components/         # React components
│   ├── App.js             # Main app component
│   └── index.js           # Entry point
├── backend/               # Python backend
│   ├── app.py            # Flask application
│   └── pdf_processor.py  # PDF processing logic
├── public/               # Static files
├── package.json          # Frontend dependencies
└── requirements.txt      # Backend dependencies
```

## Notes

- The application works without OpenAI API key using rule-based extraction
- OCR functionality requires additional system dependencies (tesseract)
- Processing time varies based on PDF complexity and size
- Maximum file size is 16MB

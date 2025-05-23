import React, { useState } from "react";
import "./ResultsDisplay.css";

const ResultsDisplay = ({ results, onReset }) => {
  const [selectedQuestion, setSelectedQuestion] = useState(null);

  const downloadJSON = () => {
    const dataStr = JSON.stringify(results, null, 2);
    const dataUri =
      "data:application/json;charset=utf-8," + encodeURIComponent(dataStr);

    const exportFileDefaultName = "parsed-questions.json";

    const linkElement = document.createElement("a");
    linkElement.setAttribute("href", dataUri);
    linkElement.setAttribute("download", exportFileDefaultName);
    linkElement.click();
  };

  if (!results || !results.questions) {
    return (
      <div className="results-container">
        <p>No questions found in the PDF.</p>
        <button onClick={onReset} className="reset-button">
          Try Another File
        </button>
      </div>
    );
  }

  return (
    <div className="results-container">
      <div className="results-header">
        <h2>Extracted Questions ({results.questions.length})</h2>
        <div className="header-actions">
          <button onClick={downloadJSON} className="download-button">
            Download JSON
          </button>
          <button onClick={onReset} className="reset-button">
            Upload New File
          </button>
        </div>
      </div>

      <div className="questions-grid">
        {results.questions.map((question, index) => (
          <div
            key={index}
            className="question-card"
            onClick={() =>
              setSelectedQuestion(selectedQuestion === index ? null : index)
            }
          >
            <div className="question-header">
              <h3>Question {question.question_number || index + 1}</h3>
              <span className="question-type">
                {question.type || "Multiple Choice"}
              </span>
            </div>

            <div className="question-preview">
              {question.question && question.question.length > 100
                ? `${question.question.substring(0, 100)}...`
                : question.question || "No question text found"}
            </div>

            {selectedQuestion === index && (
              <div className="question-details">
                <div className="question-text">
                  <h4>Question:</h4>
                  <p>{question.question}</p>
                </div>

                {question.options && question.options.length > 0 && (
                  <div className="question-options">
                    <h4>Options:</h4>
                    <ul>
                      {question.options.map((option, optIndex) => (
                        <li
                          key={optIndex}
                          className={
                            option === question.correct_answer
                              ? "correct-answer"
                              : ""
                          }
                        >
                          {option}
                          {option === question.correct_answer && " ✓"}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {question.correct_answer && (
                  <div className="correct-answer-section">
                    <h4>Correct Answer:</h4>
                    <p className="answer">{question.correct_answer}</p>
                  </div>
                )}

                {question.marks && (
                  <div className="marks-section">
                    <h4>Marks:</h4>
                    <p>{question.marks}</p>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {results.metadata && (
        <div className="metadata-section">
          <h3>Processing Information</h3>
          <div className="metadata-grid">
            {results.metadata.total_pages && (
              <div className="metadata-item">
                <strong>Total Pages:</strong> {results.metadata.total_pages}
              </div>
            )}
            {results.metadata.processing_time && (
              <div className="metadata-item">
                <strong>Processing Time:</strong>{" "}
                {results.metadata.processing_time}s
              </div>
            )}
            {results.metadata.extraction_method && (
              <div className="metadata-item">
                <strong>Extraction Method:</strong>{" "}
                {results.metadata.extraction_method}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default ResultsDisplay;

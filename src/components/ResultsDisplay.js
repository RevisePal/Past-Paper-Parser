import React, { useState, useEffect } from "react";
import { useRef } from "react";
import "./ResultsDisplay.css";

const ResultsDisplay = ({ results, onReset }) => {
  const [editingIndex, setEditingIndex] = useState(null);
  const [editedText, setEditedText] = useState("");
  const [questions, setQuestions] = useState([]);
  const [editingOptionsIndex, setEditingOptionsIndex] = useState(null);
  const [editedOptions, setEditedOptions] = useState([]);
  const textareaRef = useRef(null);

  useEffect(() => {
    if (results && results.questions) {
      setQuestions(results.questions);
    }
  }, [results]);

  const handleRemoveQuestion = (removeIndex) => {
    setQuestions((prevQuestions) =>
      prevQuestions.filter((_, idx) => idx !== removeIndex)
    );
  };

  const downloadJSON = () => {
    const now = new Date().toISOString();
    const mappedQuestions = questions.map((q, index) => ({
      isdeleted: false,
      _id: q._id || q.id || "",
      bubble_id: null,
      question: q.question || "",
      answer: q.answer || "",
      explanation: "",
      order: (index + 1) * 100,
      type: q.type === "Multiple Choice" ? "1" : "2",
      options: q.options || [],
      image: Array.isArray(q.image) ? (q.image[0] || "") : (q.image || ""),
      createdAt: now,
      __v: 0,
      calculator: null,
      checkGTP: null,
    }));
    const dataStr = JSON.stringify(mappedQuestions, null, 2);
    const dataUri =
      "data:application/json;charset=utf-8," + encodeURIComponent(dataStr);

    const exportFileDefaultName = "parsed-questions.json";

    const linkElement = document.createElement("a");
    linkElement.setAttribute("href", dataUri);
    linkElement.setAttribute("download", exportFileDefaultName);
    linkElement.click();
  };

  const handleInsertHtmlTag = (tag, closeTag) => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const currentText = editedText;

    let newText;
    let newCursorPosition;

    if (tag === "<br>") {
      // For <br>, just insert at cursor position
      newText = currentText.substring(0, start) + tag + currentText.substring(end);
      newCursorPosition = start + tag.length;
    } else if (start !== end) {
      // If text is selected, wrap it with tags
      newText = currentText.substring(0, start) +
                tag +
                currentText.substring(start, end) +
                closeTag +
                currentText.substring(end);
      newCursorPosition = end + tag.length + closeTag.length;
    } else {
      // If no text is selected, insert tags and place cursor in between
      newText = currentText.substring(0, start) +
                tag +
                closeTag +
                currentText.substring(end);
      newCursorPosition = start + tag.length;
    }

    setEditedText(newText);

    // Restore cursor position after state update
    // This needs to be done after the component re-renders
    setTimeout(() => {
      textarea.selectionStart = newCursorPosition;
      textarea.selectionEnd = newCursorPosition;
      textarea.focus();
    }, 0);
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
        <h2>Extracted Questions ({questions.length})</h2>
        <div className="header-actions">
          <button onClick={downloadJSON} className="download-button">
            Download JSON
          </button>
          <button onClick={onReset} className="reset-button">
            Upload New File
          </button>
        </div>
      </div>

      <div className="questions-list">
        {questions.map((question, index) => (
          <div key={index} className="question-card">
            <div className="question-header">
              <h3>Question {question.question_number || index + 1}</h3>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span className="question-type">
                  {question.type || "Multiple Choice"}
                </span>
                <button
                  onClick={() => handleRemoveQuestion(index)}
                  className="remove-button"
                  aria-label="Remove question"
                >
                  X
                </button>
              </div>
            </div>

            {question.image && question.image.length > 0 && (
              <div className="question-images">
                {question.image.map((src, imgIndex) => (
                  <img
                    key={imgIndex}
                    src={`http://127.0.0.1:5001${src}`}
                    alt={`Diagram ${imgIndex + 1}`}
                    className="question-image"
                  />
                ))}
              </div>
            )}

            <div className="question-text">
            {editingIndex === index ? (
  <div className="edit-question-block">
    <div className="html-toolbar">
  <button onClick={() => handleInsertHtmlTag('<b>', '</b>')} title="Bold"><b>B</b></button>
  <button onClick={() => handleInsertHtmlTag('<i>', '</i>')} title="Italic">I</button>
  <button onClick={() => handleInsertHtmlTag('<sub>', '</sub>')} title="Subscript">X₂</button>
  <button onClick={() => handleInsertHtmlTag('<sup>', '</sup>')} title="Superscript">X²</button>
  <button onClick={() => handleInsertHtmlTag('<br>', '')} title="Line Break">&lt;br&gt;</button>
</div>
    <textarea
      ref={textareaRef}
      value={editedText}
      onChange={(e) => setEditedText(e.target.value)}
      rows={5}
      className="question-editor"
    />
    <button
      onClick={() => {
        setQuestions(prevQuestions =>
          prevQuestions.map((q, i) =>
            i === index ? { ...q, question: editedText } : q
          )
        );
        setEditingIndex(null);
      }}
      className="save-button"
    >
      Save
    </button>
    <button
      onClick={() => setEditingIndex(null)}
      className="cancel-button"
    >
      Cancel
    </button>
  </div>
) : (
  <>
    <div
      className="question-html"
      dangerouslySetInnerHTML={{
        __html: question.question || "<em>No question text found</em>",
      }}
    />
    <button
      onClick={() => {
        setEditingIndex(index);
        setEditedText((question.question || "").trim());
      }}
      className="edit-button"
    >
      Edit Question
    </button>
  </>
)}
            </div>

            {question.options && question.options.length > 0 && (
              <div className="question-options">
                <h4>Options:</h4>
                {editingOptionsIndex === index ? (
                  <div className="options-editor">
                    {editedOptions.map((option, optIndex) => (
                      <div key={optIndex} className="option-edit-row">
                        <input
                          type="text"
                          value={option}
                          onChange={(e) => {
                            const updated = [...editedOptions];
                            updated[optIndex] = e.target.value;
                            setEditedOptions(updated);
                          }}
                          className="option-input"
                        />
                        <button
                          onClick={() => setEditedOptions(editedOptions.filter((_, i) => i !== optIndex))}
                          className="remove-option-button"
                        >
                          ×
                        </button>
                      </div>
                    ))}
                    <button
                      onClick={() => setEditedOptions([...editedOptions, ""])}
                      className="add-option-button"
                    >
                      + Add Option
                    </button>
                    <div>
                      <button
                        onClick={() => {
                          setQuestions(prev =>
                            prev.map((q, i) =>
                              i === index ? { ...q, options: editedOptions.filter(o => o.trim() !== "") } : q
                            )
                          );
                          setEditingOptionsIndex(null);
                        }}
                        className="save-button"
                      >
                        Save
                      </button>
                      <button onClick={() => setEditingOptionsIndex(null)} className="cancel-button">
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <ul>
                      {question.options.map((option, optIndex) => (
                        <li
                          key={optIndex}
                          className={option === question.answer ? "correct-answer" : ""}
                        >
                          <span dangerouslySetInnerHTML={{ __html: option }} />
                          {option === question.answer && " ✓"}
                        </li>
                      ))}
                    </ul>
                    <button
                      onClick={() => {
                        setEditingOptionsIndex(index);
                        setEditedOptions([...question.options]);
                      }}
                      className="edit-button"
                    >
                      Edit Options
                    </button>
                  </>
                )}
              </div>
            )}

            {question.answer && (
              <div className="correct-answer-section">
                <h4>Correct Answer:</h4>
                <p className="answer">{question.answer}</p>
              </div>
            )}

            {question.marks && (
              <div className="marks-section">
                <h4>Marks:</h4>
                <p>{question.marks}</p>
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
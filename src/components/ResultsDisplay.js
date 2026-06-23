import React, { useState, useEffect } from "react";
import { useRef } from "react";
import "./ResultsDisplay.css";

const FIREBASE_IMAGE_BASE =
  "https://firebasestorage.googleapis.com/v0/b/cloudpass-f5536.appspot.com/o/images%2F";

const bubbleLabel = (b) => `${b.name} — ${b.topic_id?.name || "Unknown topic"}`;

const isCalculatorEnabled = (q) => q.calculator === true;
const isChatGptEnabled = (q) => q.checkGTP === true;

const ResultsDisplay = ({ results, onReset }) => {
  const [editingIndex, setEditingIndex] = useState(null);
  const [editedText, setEditedText] = useState("");
  const [questions, setQuestions] = useState([]);
  const [editingOptionsIndex, setEditingOptionsIndex] = useState(null);
  const [editedOptions, setEditedOptions] = useState([]);
  const [editingAnswerIndex, setEditingAnswerIndex] = useState(null);
  const [editedAnswers, setEditedAnswers] = useState([]);
  const [copiedImage, setCopiedImage] = useState(null);
  const [bubbles, setBubbles] = useState([]);
  const [expandedBubbleIndices, setExpandedBubbleIndices] = useState(new Set());
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const uploadTargetIndex = useRef(null);

  useEffect(() => {
    if (results && results.questions) {
      try {
        const saved = localStorage.getItem("ppp_questions");
        setQuestions(saved ? JSON.parse(saved) : results.questions);
      } catch {
        setQuestions(results.questions);
      }
    }
  }, [results]);

  useEffect(() => {
    if (questions.length > 0) {
      localStorage.setItem("ppp_questions", JSON.stringify(questions));
    }
  }, [questions]);

  useEffect(() => {
    const params = new URLSearchParams();
    if (results?.metadata?.subject_id) params.set("subject_id", results.metadata.subject_id);
    if (results?.metadata?.board_id) params.set("board_id", results.metadata.board_id);
    fetch(`http://127.0.0.1:5001/api/bubbles?${params}`)
      .then((res) => res.json())
      .then((data) => setBubbles(data.bubbles || []))
      .catch((err) => console.error("Failed to load bubbles:", err));
  }, [results]);

  const handleBubbleSelectChange = (index, value) => {
    const match = bubbles.find((b) => b._id === value) || null;
    setQuestions((prev) =>
      prev.map((q, i) => (i === index ? { ...q, bubble_id: match } : q))
    );
  };

  const handleExpandBubbles = (index) => {
    setExpandedBubbleIndices((prev) => new Set(prev).add(index));
  };

  const handleToggleCalculator = (index) => {
    setQuestions((prev) =>
      prev.map((q, i) => (i === index ? { ...q, calculator: !isCalculatorEnabled(q) } : q))
    );
  };

  const handleToggleChatGpt = (index) => {
    setQuestions((prev) =>
      prev.map((q, i) => (i === index ? { ...q, checkGTP: !isChatGptEnabled(q) } : q))
    );
  };

  const handleRemoveQuestion = (removeIndex) => {
    setQuestions((prevQuestions) =>
      prevQuestions.filter((_, idx) => idx !== removeIndex)
    );
  };

  const handleRemoveImage = (questionIndex, imgIndex) => {
    setQuestions((prev) =>
      prev.map((q, i) =>
        i === questionIndex
          ? { ...q, image: q.image.filter((_, ii) => ii !== imgIndex) }
          : q
      )
    );
  };

  const handleCopyImage = (src) => {
    setCopiedImage(src);
  };

  const handlePasteImage = (questionIndex) => {
    if (!copiedImage) return;
    setQuestions((prev) =>
      prev.map((q, i) => {
        if (i !== questionIndex) return q;
        const imgs = Array.isArray(q.image) ? q.image : (q.image ? [q.image] : []);
        if (imgs.includes(copiedImage)) return q;
        return { ...q, image: [...imgs, copiedImage] };
      })
    );
  };

  const handleUploadImage = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file || uploadTargetIndex.current === null) return;
    const questionIndex = uploadTargetIndex.current;
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await fetch("http://127.0.0.1:5001/api/upload-image", {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (data.path) {
        setQuestions((prev) =>
          prev.map((q, i) => {
            if (i !== questionIndex) return q;
            const imgs = Array.isArray(q.image) ? q.image : (q.image ? [q.image] : []);
            return { ...q, image: [...imgs, data.path] };
          })
        );
      }
    } catch (err) {
      console.error("Image upload failed:", err);
    }
  };

  const handleSave = async () => {
    // 1. Upload local images to Firebase Storage
    let urlMap = {};
    try {
      const res = await fetch("http://127.0.0.1:5001/api/save-to-firebase", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ questions }),
      });
      const data = await res.json();
      urlMap = data.url_map || {};
    } catch (err) {
      console.error("Firebase upload failed:", err);
    }

    // 2. Replace local paths with Firebase URLs in questions state
    let updatedQuestions = questions;
    if (Object.keys(urlMap).length > 0) {
      updatedQuestions = questions.map((q) => {
        const imgs = Array.isArray(q.image) ? q.image : (q.image ? [q.image] : []);
        return { ...q, image: imgs.map((src) => urlMap[src] || src) };
      });
      setQuestions(updatedQuestions);
    }

    // 3. Delete local images that are no longer referenced
    const keepPaths = new Set();
    updatedQuestions.forEach((q) => {
      const imgs = Array.isArray(q.image) ? q.image : (q.image ? [q.image] : []);
      imgs.forEach((p) => { if (p) keepPaths.add(p); });
    });
    try {
      await fetch("http://127.0.0.1:5001/api/cleanup-images", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keep: [...keepPaths] }),
      });
    } catch (err) {
      console.error("Cleanup failed:", err);
    }
  };

  const typeCode = (t) =>
    t === "Multiple Choice" ? "1" : t === "Fill in the Blank" ? "2" : "3";

  const downloadJSON = () => {
    const now = new Date().toISOString();
    const mappedQuestions = questions.map((q, index) => {
      const code = typeCode(q.type);
      return {
        isdeleted: false,
        _id: q._id || q.id || "",
        bubble_id: q.bubble_id || null,
        question: q.question || "",
        ...(code === "1" ? { options: q.options || [] } : {}),
        answer: q.answer || "",
        explanation: "",
        order: (index + 1) * 100,
        type: code,
        image: Array.isArray(q.image) ? (q.image[0] || "") : (q.image || ""),
        createdAt: now,
        __v: 0,
        calculator: isCalculatorEnabled(q) ? true : null,
        checkGTP: isChatGptEnabled(q) ? true : null,
      };
    });
    const dataStr = JSON.stringify(mappedQuestions, null, 2);
    const dataUri =
      "data:application/json;charset=utf-8," + encodeURIComponent(dataStr);

    const exportFileDefaultName = "parsed-questions.json";

    const linkElement = document.createElement("a");
    linkElement.setAttribute("href", dataUri);
    linkElement.setAttribute("download", exportFileDefaultName);
    linkElement.click();
  };

  const handleInsertHtmlTag = (tag, closeTag, ref = textareaRef, getText = () => editedText, setText = setEditedText) => {
    const textarea = ref.current;
    if (!textarea) return;

    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const currentText = getText();

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

    setText(newText);

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

            <div className="bubble-picker">
              <label htmlFor={`bubble-select-${index}`}>Topic (bubble):</label>
              <select
                id={`bubble-select-${index}`}
                value={question.bubble_id?._id || ""}
                onChange={(e) => handleBubbleSelectChange(index, e.target.value)}
                className="bubble-select"
              >
                <option value="">— No topic selected —</option>
                {(expandedBubbleIndices.has(index)
                  ? bubbles
                  : bubbles.filter((b) => b._id === question.bubble_id?._id)
                ).map((b) => (
                  <option key={b._id} value={b._id}>
                    {bubbleLabel(b)}
                  </option>
                ))}
              </select>
              {expandedBubbleIndices.has(index) ? (
                <span className="bubble-expanded-hint">
                  Showing all {bubbles.length} topics
                </span>
              ) : (
                <button
                  type="button"
                  className="load-all-bubbles-button"
                  onClick={() => handleExpandBubbles(index)}
                >
                  Show all {bubbles.length} topics
                </button>
              )}
              <span className={question.bubble_id ? "bubble-matched" : "bubble-unmatched"}>
                {question.bubble_id ? "✓ matched" : "No topic selected"}
              </span>
            </div>

            <div className="question-toggles">
              <label className="toggle-checkbox">
                <input
                  type="checkbox"
                  checked={isCalculatorEnabled(question)}
                  onChange={() => handleToggleCalculator(index)}
                />
                Calculator allowed
              </label>
              <label className="toggle-checkbox">
                <input
                  type="checkbox"
                  checked={isChatGptEnabled(question)}
                  onChange={() => handleToggleChatGpt(index)}
                />
                AI-assisted grading (ChatGPT)
              </label>
            </div>

            <div className="question-images">
              {question.image && question.image.map((src, imgIndex) => (
                <div key={imgIndex} className="question-image-wrapper">
                  <img
                    src={
                      src.startsWith("/images/")
                        ? `${FIREBASE_IMAGE_BASE}${src.slice("/images/".length)}?alt=media`
                        : `http://127.0.0.1:5001${src}`
                    }
                    alt={`Diagram ${imgIndex + 1}`}
                    className="question-image"
                  />
                  <button
                    className="copy-image-button"
                    onClick={() => handleCopyImage(src)}
                    aria-label="Copy image"
                    title="Copy image to clipboard"
                  >
                    {copiedImage === src ? "Copied" : "Copy"}
                  </button>
                  <button
                    className="remove-image-button"
                    onClick={() => handleRemoveImage(index, imgIndex)}
                    aria-label="Remove image"
                  >
                    ×
                  </button>
                </div>
              ))}
              <div className="image-actions">
                <button
                  className="upload-image-button"
                  onClick={() => {
                    uploadTargetIndex.current = index;
                    fileInputRef.current.click();
                  }}
                >
                  + Upload Image
                </button>
                {copiedImage && (
                  <button
                    className="paste-image-button"
                    onClick={() => handlePasteImage(index)}
                    title="Paste copied image into this question"
                  >
                    + Paste Image
                  </button>
                )}
              </div>
            </div>

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
                          className={
                            Array.isArray(question.answer)
                              ? question.answer.includes(option) ? "correct-answer" : ""
                              : option === question.answer ? "correct-answer" : ""
                          }
                        >
                          <span dangerouslySetInnerHTML={{ __html: option }} />
                          {(Array.isArray(question.answer) ? question.answer.includes(option) : option === question.answer) && " ✓"}
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

            <div className="correct-answer-section">
              <h4>Correct Answer:</h4>
              {editingAnswerIndex === index ? (
                <div className="options-editor">
                  {editedAnswers.map((ans, ansIdx) => (
                    <div key={ansIdx} className="option-edit-row">
                      <input
                        type="text"
                        value={ans}
                        onChange={(e) => {
                          const updated = [...editedAnswers];
                          updated[ansIdx] = e.target.value;
                          setEditedAnswers(updated);
                        }}
                        className="option-input"
                      />
                      <button
                        onClick={() => setEditedAnswers(editedAnswers.filter((_, i) => i !== ansIdx))}
                        className="remove-option-button"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  <button
                    onClick={() => setEditedAnswers([...editedAnswers, ""])}
                    className="add-option-button"
                  >
                    + Add Answer
                  </button>
                  <div>
                    <button
                      onClick={() => {
                        const filtered = editedAnswers.filter(a => a.trim() !== "");
                        setQuestions(prev =>
                          prev.map((q, i) =>
                            i === index ? { ...q, answer: filtered.length === 1 ? filtered[0] : filtered } : q
                          )
                        );
                        setEditingAnswerIndex(null);
                      }}
                      className="save-button"
                    >
                      Save
                    </button>
                    <button onClick={() => setEditingAnswerIndex(null)} className="cancel-button">
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  {Array.isArray(question.answer) ? (
                    question.answer.length > 0 ? (
                      <ul className="answer-list">
                        {question.answer.map((a, i) => <li key={i}>{a}</li>)}
                      </ul>
                    ) : (
                      <p className="answer">Not set</p>
                    )
                  ) : (
                    <p className="answer">{question.answer || "Not set"}</p>
                  )}
                  <button
                    onClick={() => {
                      setEditingAnswerIndex(index);
                      const ans = question.answer;
                      setEditedAnswers(
                        Array.isArray(ans) ? [...ans] : ans ? [ans] : [""]
                      );
                    }}
                    className="edit-button"
                  >
                    Edit Answer
                  </button>
                </>
              )}
            </div>
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
      <div className="save-section">
        <button onClick={handleSave} className="save-all-button">
          Save
        </button>
      </div>
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        style={{ display: "none" }}
        onChange={handleUploadImage}
      />
    </div>
  );
};

export default ResultsDisplay;
import React, { useState, useEffect } from "react";
import { useRef } from "react";
import ReactCrop from "react-image-crop";
import "react-image-crop/dist/ReactCrop.css";
import "./ResultsDisplay.css";
import { toAppPlainText, toAppQuestionHtml, toSubscript, toSuperscript } from "../utils/scripts";

const FIREBASE_IMAGE_BASE =
  "https://firebasestorage.googleapis.com/v0/b/cloudpass-f5536.appspot.com/o/images%2F";

const bubbleLabel = (b) => `${b.name} — ${b.topic_id?.name || "Unknown topic"}`;

const isCalculatorEnabled = (q) => q.calculator === true;
const isChatGptEnabled = (q) => q.checkGTP === true;
const isMultiSelectEnabled = (q) => q.multiSelect === true;

// "Ã" / "Â" turning up as a lead byte almost always means a PDF/AI encoding
// glitch (e.g. × or ÷ mis-generated) — these letters don't otherwise appear
// in English-language exam content, so this is a reliable, low-noise flag.
const MOJIBAKE_PATTERN = /[ÃÂ]/;

const hasSuspiciousEncoding = (q) => {
  const texts = [
    q.question || "",
    ...(q.options || []),
    ...(Array.isArray(q.answer) ? q.answer : [q.answer || ""]),
  ];
  return texts.some((t) => MOJIBAKE_PATTERN.test(t));
};

const loadCachedBatchMeta = () => {
  try {
    return JSON.parse(localStorage.getItem("ppp_batch_meta"));
  } catch {
    return null;
  }
};

const ResultsDisplay = ({ results, onReset }) => {
  const [editingIndex, setEditingIndex] = useState(null);
  const [editedText, setEditedText] = useState("");
  const [questions, setQuestions] = useState(() => {
    try {
      const saved = localStorage.getItem("ppp_questions");
      return saved ? JSON.parse(saved) : results?.questions || [];
    } catch {
      return results?.questions || [];
    }
  });
  const [editingOptionsIndex, setEditingOptionsIndex] = useState(null);
  const [editedOptions, setEditedOptions] = useState([]);
  const [editingAnswerIndex, setEditingAnswerIndex] = useState(null);
  const [editedAnswers, setEditedAnswers] = useState([]);
  const [copiedImage, setCopiedImage] = useState(null);
  const [bubbles, setBubbles] = useState([]);
  const [expandedBubbleIndices, setExpandedBubbleIndices] = useState(new Set());
  const [jobId, setJobId] = useState(
    () => loadCachedBatchMeta()?.jobId ?? results?.metadata?.job_id ?? null
  );
  const [hasMore, setHasMore] = useState(
    () => loadCachedBatchMeta()?.hasMore ?? results?.metadata?.has_more ?? false
  );
  const [nextStartPage, setNextStartPage] = useState(
    () => loadCachedBatchMeta()?.nextStartPage ?? results?.metadata?.end_page ?? 0
  );
  const [totalPages, setTotalPages] = useState(
    () => loadCachedBatchMeta()?.totalPages ?? results?.metadata?.total_pages ?? 0
  );
  const [extractingMore, setExtractingMore] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState(null);
  const [showJsonViewer, setShowJsonViewer] = useState(false);
  const [jsonCopied, setJsonCopied] = useState(false);
  const [pendingDelete, setPendingDelete] = useState(null);
  const [cropTarget, setCropTarget] = useState(null); // { questionIndex, imgIndex, src }
  const [crop, setCrop] = useState();
  const [completedCrop, setCompletedCrop] = useState(null);
  const [cropSaving, setCropSaving] = useState(false);
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const uploadTargetIndex = useRef(null);
  const undoTimeoutRef = useRef(null);
  const cropImgRef = useRef(null);

  useEffect(() => {
    return () => {
      if (undoTimeoutRef.current) clearTimeout(undoTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    localStorage.setItem(
      "ppp_batch_meta",
      JSON.stringify({ jobId, hasMore, nextStartPage, totalPages })
    );
  }, [jobId, hasMore, nextStartPage, totalPages]);

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

  const handleToggleMultiSelect = (index) => {
    setQuestions((prev) =>
      prev.map((q, i) => (i === index ? { ...q, multiSelect: !isMultiSelectEnabled(q) } : q))
    );
  };

  const handleExtractMore = async () => {
    if (!jobId || extractingMore) return;
    setExtractingMore(true);
    try {
      const res = await fetch("http://127.0.0.1:5001/api/process-pdf-batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: jobId,
          start_page: nextStartPage,
          subject_id: results?.metadata?.subject_id || "",
          board_id: results?.metadata?.board_id || "",
        }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      setQuestions((prev) => [...prev, ...(data.questions || [])]);
      const meta = data.metadata || {};
      setHasMore(meta.has_more || false);
      setNextStartPage(meta.end_page ?? nextStartPage);
      setJobId(meta.job_id ?? null);
    } catch (err) {
      console.error("Extract more failed:", err);
    } finally {
      setExtractingMore(false);
    }
  };

  const UNDO_TIMEOUT_MS = 6000;

  const handleRemoveQuestion = (removeIndex) => {
    const removedQuestion = questions[removeIndex];
    setQuestions((prevQuestions) =>
      prevQuestions.filter((_, idx) => idx !== removeIndex)
    );

    if (undoTimeoutRef.current) clearTimeout(undoTimeoutRef.current);
    setPendingDelete({ question: removedQuestion, index: removeIndex });
    undoTimeoutRef.current = setTimeout(() => {
      setPendingDelete(null);
      undoTimeoutRef.current = null;
    }, UNDO_TIMEOUT_MS);
  };

  const handleUndoRemove = () => {
    if (!pendingDelete) return;
    if (undoTimeoutRef.current) {
      clearTimeout(undoTimeoutRef.current);
      undoTimeoutRef.current = null;
    }
    setQuestions((prev) => {
      const next = [...prev];
      next.splice(pendingDelete.index, 0, pendingDelete.question);
      return next;
    });
    setPendingDelete(null);
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

  const onCropImageLoad = (e) => {
    const { width, height } = e.currentTarget;
    const x = width * 0.05;
    const y = height * 0.05;
    const initial = { unit: "px", x, y, width: width * 0.9, height: height * 0.9 };
    setCrop(initial);
    setCompletedCrop(initial);
  };

  const handleCancelCrop = () => {
    setCropTarget(null);
    setCrop(undefined);
    setCompletedCrop(null);
  };

  const handleApplyCrop = async () => {
    const image = cropImgRef.current;
    if (!image || !completedCrop || !cropTarget) return;
    if (!completedCrop.width || !completedCrop.height) return;

    const scaleX = image.naturalWidth / image.width;
    const scaleY = image.naturalHeight / image.height;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(completedCrop.width * scaleX);
    canvas.height = Math.round(completedCrop.height * scaleY);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(
      image,
      completedCrop.x * scaleX,
      completedCrop.y * scaleY,
      completedCrop.width * scaleX,
      completedCrop.height * scaleY,
      0,
      0,
      canvas.width,
      canvas.height
    );

    setCropSaving(true);
    try {
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("Failed to generate cropped image");

      const formData = new FormData();
      formData.append("file", blob, "cropped.png");
      const res = await fetch("http://127.0.0.1:5001/api/upload-image", {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (!data.path) throw new Error(data.error || "Upload failed");

      const { questionIndex, imgIndex } = cropTarget;
      setQuestions((prev) =>
        prev.map((q, i) => {
          if (i !== questionIndex) return q;
          const imgs = Array.isArray(q.image) ? q.image : (q.image ? [q.image] : []);
          return { ...q, image: imgs.map((p, ii) => (ii === imgIndex ? data.path : p)) };
        })
      );
      handleCancelCrop();
    } catch (err) {
      console.error("Crop save failed:", err);
    } finally {
      setCropSaving(false);
    }
  };

  const handleSave = async () => {
    setSaveStatus(null);
    setSaving(true);

    // 1. Upload local images to Firebase Storage
    let urlMap = {};
    let missingImages = [];
    let firebaseError = null;
    try {
      const res = await fetch("http://127.0.0.1:5001/api/save-to-firebase", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ questions }),
      });
      const data = await res.json();
      if (!res.ok) {
        firebaseError = data.error || "Failed to save images to Firebase.";
      } else {
        urlMap = data.url_map || {};
        missingImages = data.missing || [];
      }
    } catch (err) {
      console.error("Firebase upload failed:", err);
      firebaseError = "Could not reach the backend to save images.";
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

    setSaving(false);
    if (firebaseError) {
      setSaveStatus({ type: "error", message: firebaseError });
    } else if (missingImages.length > 0) {
      setSaveStatus({
        type: "error",
        message: `Saved, but ${missingImages.length} image(s) could no longer be found on the server and were skipped — their questions kept the old (now broken) image link. This usually means the local image cache was cleared since you extracted them; re-extracting or re-uploading those images should fix it.`,
      });
    } else {
      setSaveStatus({
        type: "success",
        message: `Saved — uploaded ${Object.keys(urlMap).length} image(s) to Firebase.`,
      });
    }
  };

  const typeCode = (t) =>
    t === "Multiple Choice" ? "1" : t === "Fill in the Blank" ? "2" : "3";

  const buildExportQuestions = () => {
    const now = new Date().toISOString();
    return questions.map((q, index) => {
      const code = typeCode(q.type);
      return {
        isdeleted: false,
        _id: q._id || q.id || "",
        bubble_id: q.bubble_id || null,
        // Only export markup the mobile app can display (see utils/scripts.js).
        // Options and answers go through the same function so multiple-choice
        // answers still match their option strings exactly.
        question: toAppQuestionHtml(q.question || ""),
        ...(code === "1" ? { options: (q.options || []).map(toAppPlainText) } : {}),
        answer: Array.isArray(q.answer)
          ? q.answer.map(toAppPlainText)
          : toAppPlainText(q.answer || ""),
        explanation: "",
        order: (index + 1) * 100,
        type: code,
        // Kept as an array — the mobile app now renders/fetches all images for a
        // question, not just the first one.
        image: Array.isArray(q.image) ? q.image.filter(Boolean) : q.image ? [q.image] : [],
        createdAt: now,
        __v: 0,
        calculator: isCalculatorEnabled(q) ? true : null,
        checkGTP: isChatGptEnabled(q) ? true : null,
        multiSelect: code === "1" && isMultiSelectEnabled(q) ? true : null,
      };
    });
  };

  const downloadJSON = () => {
    const dataStr = JSON.stringify(buildExportQuestions(), null, 2);
    const dataUri =
      "data:application/json;charset=utf-8," + encodeURIComponent(dataStr);

    const exportFileDefaultName = "parsed-questions.json";

    const linkElement = document.createElement("a");
    linkElement.setAttribute("href", dataUri);
    linkElement.setAttribute("download", exportFileDefaultName);
    linkElement.click();
  };

  const handleCopyJSON = () => {
    navigator.clipboard.writeText(JSON.stringify(buildExportQuestions(), null, 2)).then(() => {
      setJsonCopied(true);
      setTimeout(() => setJsonCopied(false), 1500);
    });
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

  // Rewrites the selected text as Unicode super/subscript characters (which
  // the mobile app can display) rather than wrapping it in <sup>/<sub> tags.
  const handleConvertSelection = (convert) => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const { selectionStart: start, selectionEnd: end } = textarea;
    if (start === end) {
      textarea.focus();
      return;
    }
    const converted = convert(editedText.substring(start, end));
    setEditedText(editedText.substring(0, start) + converted + editedText.substring(end));
    setTimeout(() => {
      textarea.selectionStart = textarea.selectionEnd = start + converted.length;
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
          <button onClick={() => setShowJsonViewer(true)} className="view-json-button">
            View JSON
          </button>
          <button onClick={downloadJSON} className="download-button">
            Download JSON
          </button>
          <button onClick={onReset} className="reset-button">
            Upload New File
          </button>
        </div>
      </div>

      {showJsonViewer && (
        <div className="json-modal-overlay" onClick={() => setShowJsonViewer(false)}>
          <div className="json-modal" onClick={(e) => e.stopPropagation()}>
            <div className="json-modal-header">
              <h3>Questions JSON</h3>
              <div className="json-modal-actions">
                <button onClick={handleCopyJSON} className="copy-json-button">
                  {jsonCopied ? "Copied!" : "Copy"}
                </button>
                <button
                  onClick={() => setShowJsonViewer(false)}
                  className="close-json-button"
                  aria-label="Close"
                >
                  ×
                </button>
              </div>
            </div>
            <pre className="json-modal-content">
              {JSON.stringify(buildExportQuestions(), null, 2)}
            </pre>
          </div>
        </div>
      )}

      {cropTarget && (
        <div className="crop-modal-overlay" onClick={handleCancelCrop}>
          <div className="crop-modal" onClick={(e) => e.stopPropagation()}>
            <div className="crop-modal-header">
              <h3>Crop Image</h3>
              <button onClick={handleCancelCrop} className="close-json-button" aria-label="Close">
                ×
              </button>
            </div>
            <div className="crop-modal-content">
              <ReactCrop
                crop={crop}
                onChange={(_, percentCrop) => setCrop(percentCrop)}
                onComplete={(c) => setCompletedCrop(c)}
              >
                <img
                  ref={cropImgRef}
                  src={
                    cropTarget.src.startsWith("/images/")
                      ? `${FIREBASE_IMAGE_BASE}${cropTarget.src.slice("/images/".length)}?alt=media`
                      : `http://127.0.0.1:5001${cropTarget.src}`
                  }
                  alt="Crop target"
                  crossOrigin="anonymous"
                  onLoad={onCropImageLoad}
                  style={{ maxWidth: "100%", maxHeight: "70vh" }}
                />
              </ReactCrop>
            </div>
            <div className="crop-modal-actions">
              <button onClick={handleCancelCrop} className="cancel-crop-button" disabled={cropSaving}>
                Cancel
              </button>
              <button onClick={handleApplyCrop} className="apply-crop-button" disabled={cropSaving}>
                {cropSaving ? "Saving..." : "Apply Crop"}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="questions-list">
        {questions.map((question, index) => (
          <div key={index} className="question-card">
            <div className="question-header">
              <h3>Question {question.question_number || index + 1}</h3>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span className="question-type">
                  {question.type || "Multiple Choice"}
                </span>
                {hasSuspiciousEncoding(question) && (
                  <span
                    className="encoding-warning"
                    title="Contains 'Ã' or 'Â' — usually a sign a special character (e.g. ×, ÷) got mis-generated. Please check the text."
                  >
                    ⚠ Check encoding
                  </span>
                )}
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
              {question.type === "Multiple Choice" && (
                <label className="toggle-checkbox">
                  <input
                    type="checkbox"
                    checked={isMultiSelectEnabled(question)}
                    onChange={() => handleToggleMultiSelect(index)}
                  />
                  Multiple correct answers (select all that apply)
                  {isMultiSelectEnabled(question) &&
                    (!Array.isArray(question.answer) || question.answer.length < 2) && (
                      <span className="multiselect-warning">
                        ⚠ needs 2+ correct answers set below
                      </span>
                    )}
                </label>
              )}
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
                    className="crop-image-button"
                    onClick={() => {
                      setCrop(undefined);
                      setCompletedCrop(null);
                      setCropTarget({ questionIndex: index, imgIndex, src });
                    }}
                    aria-label="Crop image"
                    title="Crop image"
                  >
                    Crop
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
  <button onClick={() => handleInsertHtmlTag('<strong>', '</strong>')} title="Bold"><b>B</b></button>
  <button onClick={() => handleInsertHtmlTag('<em>', '</em>')} title="Italic">I</button>
  <button onClick={() => handleConvertSelection(toSubscript)} title="Subscript: select text first (e.g. the 2 in CO2)">X₂</button>
  <button onClick={() => handleConvertSelection(toSuperscript)} title="Superscript: select text first (e.g. the 3 in cm3; a lone o becomes °)">X²</button>
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
        setEditedText(toAppQuestionHtml(question.question || "").trim());
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

      {hasMore && (
        <div className="extract-more-section">
          <p className="extract-more-progress">
            Extracted pages 1–{nextStartPage} of {totalPages}
          </p>
          <button
            onClick={handleExtractMore}
            disabled={extractingMore}
            className="extract-more-button"
          >
            {extractingMore
              ? "Extracting..."
              : `Extract Next ${Math.min(10, totalPages - nextStartPage)} Pages (${nextStartPage + 1}–${Math.min(nextStartPage + 10, totalPages)})`}
          </button>
        </div>
      )}

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
        {saveStatus && (
          <p className={saveStatus.type === "error" ? "save-status-error" : "save-status-success"}>
            {saveStatus.message}
          </p>
        )}
        <button onClick={handleSave} disabled={saving} className="save-all-button">
          {saving ? "Saving..." : "Save"}
        </button>
      </div>
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        style={{ display: "none" }}
        onChange={handleUploadImage}
      />

      {pendingDelete && (
        <div className="undo-toast" role="status">
          <span>Question removed.</span>
          <button onClick={handleUndoRemove} className="undo-button">
            Undo
          </button>
        </div>
      )}
    </div>
  );
};

export default ResultsDisplay;
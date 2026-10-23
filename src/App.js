import React, { useState, useEffect } from "react";
import FileUpload from "./components/FileUpload";
import ResultsDisplay from "./components/ResultsDisplay";
import "./App.css";

function App() {
  const [results, setResults] = useState(() => {
    try {
      const saved = localStorage.getItem("ppp_results");
      return saved ? JSON.parse(saved) : null;
    } catch {
      return null;
    }
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [examboards, setExamboards] = useState([]);
  const [subjects, setSubjects] = useState([]);
  const [topics, setTopics] = useState([]);
  const [selectedBoard, setSelectedBoard] = useState("");
  const [selectedSubject, setSelectedSubject] = useState("");
  const [selectedTopic, setSelectedTopic] = useState("");

  useEffect(() => {
    if (results) {
      localStorage.setItem("ppp_results", JSON.stringify(results));
    }
  }, [results]);

  useEffect(() => {
    fetch("http://127.0.0.1:5001/api/examboards")
      .then((res) => res.json())
      .then((data) => setExamboards(data.examboards || []))
      .catch((err) => console.error("Failed to load exam boards:", err));
    fetch("http://127.0.0.1:5001/api/subjects")
      .then((res) => res.json())
      .then((data) => setSubjects(data.subjects || []))
      .catch((err) => console.error("Failed to load subjects:", err));
  }, []);

  // Topic is optional and only meant to narrow an already-chosen subject —
  // without one there's no useful way to scope the topic list, so leave it
  // empty rather than fetching every topic across every subject.
  useEffect(() => {
    if (!selectedBoard || !selectedSubject) {
      setTopics([]);
      return;
    }
    const params = new URLSearchParams({ board_id: selectedBoard, subject_id: selectedSubject });
    fetch(`http://127.0.0.1:5001/api/topics?${params}`)
      .then((res) => res.json())
      .then((data) => setTopics(data.topics || []))
      .catch((err) => console.error("Failed to load topics:", err));
  }, [selectedBoard, selectedSubject]);

  const subjectsForBoard = subjects.filter(
    (s) => !selectedBoard || (s.available_board_ids || []).includes(selectedBoard)
  );

  const handleFileProcessed = (data) => {
    localStorage.removeItem("ppp_questions");
    localStorage.removeItem("ppp_batch_meta");
    setResults(data);
    setError(null);
  };

  const handleError = (errorMessage) => {
    setError(errorMessage);
    setResults(null);
  };

  const handleReset = () => {
    localStorage.removeItem("ppp_results");
    localStorage.removeItem("ppp_questions");
    localStorage.removeItem("ppp_batch_meta");
    setResults(null);
    setError(null);
    setLoading(false);
  };

  return (
    <div className="App">
      <header className="App-header">
        <h1>Past Paper Parser</h1>
        <p>Upload a PDF exam paper to extract structured questions</p>
      </header>

      <main className="App-main">
        {!results && !loading && (
          <div className="upload-section">
            <div className="taxonomy-picker">
              <div className="taxonomy-field">
                <label htmlFor="board-select">Exam Board:</label>
                <select
                  id="board-select"
                  value={selectedBoard}
                  onChange={(e) => {
                    setSelectedBoard(e.target.value);
                    setSelectedSubject("");
                    setSelectedTopic("");
                  }}
                >
                  <option value="">Select exam board...</option>
                  {examboards.map((b) => (
                    <option key={b._id} value={b._id}>
                      {b.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="taxonomy-field">
                <label htmlFor="subject-select">Subject:</label>
                <select
                  id="subject-select"
                  value={selectedSubject}
                  onChange={(e) => {
                    setSelectedSubject(e.target.value);
                    setSelectedTopic("");
                  }}
                  disabled={!selectedBoard}
                >
                  <option value="">Select subject...</option>
                  {subjectsForBoard.map((s) => (
                    <option key={s._id} value={s._id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="taxonomy-field">
                <label htmlFor="topic-select">Topic (optional):</label>
                <select
                  id="topic-select"
                  value={selectedTopic}
                  onChange={(e) => setSelectedTopic(e.target.value)}
                  disabled={!selectedSubject}
                >
                  <option value="">Any topic</option>
                  {topics.map((t) => (
                    <option key={t._id} value={t._id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <FileUpload
              onFileProcessed={handleFileProcessed}
              onError={handleError}
              onLoadingChange={setLoading}
              subjectId={selectedSubject}
              boardId={selectedBoard}
              topicId={selectedTopic}
              disabled={!selectedBoard || !selectedSubject}
            />
          </div>
        )}

        {loading && (
          <div className="loading-container">
            <div className="spinner"></div>
            <p>Processing your PDF... This may take a few moments.</p>
          </div>
        )}

        {error && (
          <div className="error-container">
            <h3>Error</h3>
            <p>{error}</p>
            <button onClick={handleReset} className="reset-button">
              Try Again
            </button>
          </div>
        )}

        {results && <ResultsDisplay results={results} onReset={handleReset} />}
      </main>
    </div>
  );
}

export default App;

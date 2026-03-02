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

  useEffect(() => {
    if (results) {
      localStorage.setItem("ppp_results", JSON.stringify(results));
    }
  }, [results]);

  const handleFileProcessed = (data) => {
    localStorage.removeItem("ppp_questions");
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
          <FileUpload
            onFileProcessed={handleFileProcessed}
            onError={handleError}
            onLoadingChange={setLoading}
          />
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

import React, { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import axios from "axios";
import "./FileUpload.css";

const FileUpload = ({ onFileProcessed, onError, onLoadingChange, subjectId, boardId, disabled }) => {
  const onDrop = useCallback(
    async (acceptedFiles) => {
      if (disabled) {
        onError("Please select an exam board and subject first");
        return;
      }

      const file = acceptedFiles[0];

      if (!file) return;

      if (file.type !== "application/pdf") {
        onError("Please upload a PDF file");
        return;
      }

      onLoadingChange(true);

      const formData = new FormData();
      formData.append("file", file);
      formData.append("subject_id", subjectId || "");
      formData.append("board_id", boardId || "");

      try {
        const response = await axios.post(
          'http://127.0.0.1:5001/api/process-pdf',
          formData,
          {
            headers: {
              "Content-Type": "multipart/form-data",
            },
            timeout: 300000,
          }
        );

        onFileProcessed(response.data);
      } catch (error) {
        console.error("Error processing file:", error);
        onError(
          error.response?.data?.error ||
            "Failed to process PDF. Please try again."
        );
      } finally {
        onLoadingChange(false);
      }
    },
    [onFileProcessed, onError, onLoadingChange, subjectId, boardId, disabled]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      "application/pdf": [".pdf"],
    },
    multiple: false,
    disabled,
  });

  return (
    <div className="file-upload-container">
      <div
        {...getRootProps()}
        className={`dropzone ${isDragActive ? "active" : ""} ${disabled ? "disabled" : ""}`}
      >
        <input {...getInputProps()} />
        <div className="upload-content">
          <div className="upload-icon">📄</div>
          {disabled ? (
            <p>Select an exam board and subject above to enable upload</p>
          ) : isDragActive ? (
            <p>Drop the PDF here...</p>
          ) : (
            <>
              <h3>Upload Past Paper PDF</h3>
              <p>Drag and drop a PDF file here, or click to select</p>
              <button className="upload-button">Choose File</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default FileUpload;

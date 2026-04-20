from flask import Flask, request, jsonify, render_template, session
import os
import json
import uuid

from agents.intake_agent import run_intake, generate_questions
from agents.analysis_agent import run_analysis
from memory.user_memory import save_profile, get_profile_context, save_analysis, get_previous_analysis

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'finance-advisor-dev-key')

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Temp storage for parsed summaries between the two steps
# In production this would move to Redis or a DB — for now, in-memory dict keyed by session ID
_temp_summaries = {}


@app.route('/')
def index():
    return render_template('dashboard.html')


@app.route('/upload', methods=['POST'])
def upload():
    """
    Step 1: Receive CSV files, run intake parsing, generate clarifying questions.
    Returns questions for the user to answer before full analysis runs.
    """
    files = request.files.getlist('file')
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    # Save uploaded files
    filepaths = []
    filenames = []
    for f in files:
        filename = f.filename
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        f.save(filepath)
        filepaths.append(filepath)
        filenames.append(filename)

    # Run intake parsing
    try:
        summary = run_intake(filepaths)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Store summary in temp dict with a session key
    session_id = str(uuid.uuid4())
    _temp_summaries[session_id] = {
        "summary": summary,
        "filenames": filenames
    }

    # Generate smart questions from Claude based on the data
    questions = generate_questions(summary)

    return jsonify({
        "session_id": session_id,
        "questions": questions,
        "date_range": summary.get("date_range")
    })


@app.route('/submit-answers', methods=['POST'])
def submit_answers():
    """
    Step 2: Receive user's answers to questions, run full analysis, return results.
    """
    body = request.get_json()
    session_id = body.get("session_id")
    answers = body.get("answers", {})

    if not session_id or session_id not in _temp_summaries:
        return jsonify({"error": "Session expired or invalid. Please re-upload your files."}), 400

    stored = _temp_summaries.pop(session_id)  # Remove after use
    summary = stored["summary"]
    filenames = stored["filenames"]

    # Save answers to persistent memory
    save_profile(answers)

    # Get full profile context (includes past sessions + current answers)
    profile_context = get_profile_context()

    # Grab previous analysis before saving the new one
    previous = get_previous_analysis()

    # Run full analysis
    result = run_analysis(summary, profile_context=profile_context)

    # Save analysis to history
    date_range = summary.get("date_range")
    save_analysis(result, filenames, date_range=date_range)

    return jsonify({"current": result, "previous": previous})


if __name__ == '__main__':
    app.run(debug=True)

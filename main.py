import uuid
import os
import bot_logic as google_bot_logic
import teams_bot_logic
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from typing import Annotated
from pydantic.functional_validators import AfterValidator
from fastapi.middleware.cors import CORSMiddleware

def get_platform(url: str) -> str:
    if "meet.google.com" in url:
        return "google"
    elif "teams.live.com" in url:
        return "teams"
    else:
        return "unsupported"

def check_url(url: str) -> str:
    platform = get_platform(url)
    if platform == "unsupported":
        raise ValueError("URL must be a valid Google Meet or Microsoft Teams link")
    return url

Url = Annotated[str, AfterValidator(check_url)]

app = FastAPI()

origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory dictionary to store job statuses.
jobs = {}

class MeetingRequest(BaseModel):
    meeting_url: Url

@app.post("/start-meeting")
async def start_meeting(request: MeetingRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}

    platform = get_platform(request.meeting_url)

    if platform == "google":
        background_tasks.add_task(google_bot_logic.run_bot_task, request.meeting_url, job_id, jobs)
    elif platform == "teams":
        background_tasks.add_task(teams_bot_logic.run_bot_task, request.meeting_url, job_id, jobs)
    
    return {"message": f"Meeting bot started for {platform}.", "job_id": job_id}

@app.post("/stop-meeting/{job_id}")
async def stop_meeting(job_id: str):
    """
    Signals the bot to gracefully exit the meeting.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("status") in ["starting_browser", "navigating", "recording"]:
        jobs[job_id]["status"] = "stopping"
        return {"message": "Stop signal sent to bot."}
    
    return {"message": f"Bot is not in an active state to be stopped. Current status: {job.get('status')}"}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/transcript/{job_id}")
async def get_transcript(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.get("status") != "completed":
        raise HTTPException(status_code=400, detail=f"Job is not complete. Current status: {job.get('status')}")

    transcript_path = job.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        raise HTTPException(status_code=404, detail="Transcript file not found.")

    return FileResponse(transcript_path, media_type='text/plain', filename='transcript.txt')

@app.get("/summary/{job_id}")
async def get_summary(job_id: str):
    """
    Retrieves the PDF summary file for a completed job.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.get("status") != "completed":
        raise HTTPException(status_code=400, detail=f"Job is not complete. Current status: {job.get('status')}")

    summary_path = job.get("summary_path")
    if not summary_path or not os.path.exists(summary_path):
        raise HTTPException(status_code=404, detail="Summary file not found.")

    return FileResponse(summary_path, media_type='application/pdf', filename='summary.pdf')
